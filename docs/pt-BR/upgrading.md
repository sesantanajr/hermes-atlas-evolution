# Atualização

[English](../en-US/upgrading.md) | **Português (Brasil)**

## Atualizar o plugin

O plugin nunca se atualiza sozinho e nunca acessa a rede: código novo só vem do Hermes. Todo comando diz o perfil em que roda: `default` para o perfil raiz, ou o nome do perfil.

```sh
hermes -p <perfil> evolution update --all          # o `plugins update` do próprio Hermes em todos os perfis, este primeiro
hermes -p <perfil> evolution update                # escolha os perfis numa lista
hermes -p <perfil> evolution update --list         # versão, commit e fixação de cada perfil
sh install.sh --all --yes --restart                # os comandos do próprio Hermes, sem o plugin carregado
hermes gateway restart
```

O `evolution update` é uma comodidade: roda o comando do próprio Hermes em cada perfil que você escolher, primeiro o perfil em que você o roda, e nunca escolhe um commit.

| A cópia num perfil | O que o `evolution update` roda ali |
|---|---|
| Instalada pelo catálogo de plugins do Hermes | `plugins update hermes-atlas-evolution`: o Hermes a passa para a versão revisada do catálogo. |
| Instalada pelo GitHub | `plugins update hermes-atlas-evolution`: o Hermes puxa a origem de onde ela foi instalada. |
| Fixada pelo Hermes com `--ref` | Só passa para o commit que o perfil em que você roda o comando roda (`plugins install <url> --force --ref <esse commit>`), e só quando as duas cópias vêm deste repositório. Senão, fica como está, com o comando que a move. |
| Ausente, com `--install-missing` | O código que o perfil em que você roda o comando roda: o nome no catálogo, ou este repositório no commit desse perfil. |
| Um link de desenvolvimento, ou a cópia fixada ou pela metade de um fork | Nunca é mexida. |

O `update` só instala onde o plugin já está; acrescente `--install-missing` para instalá-lo nos perfis escolhidos que não o têm, e `--yes` para pular a pergunta. O `install.sh` faz o mesmo só com comandos do próprio Hermes: `plugins update hermes-atlas-evolution` onde o plugin está, `plugins install` onde ele falta (com `--install-missing`; pelo GitHub, ou pelo nome no catálogo com `--catalog`), e uma cópia fixada fica como está, com o comando que a move.

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

**As atualizações sem ninguém por perto são do próprio Hermes.** O `hermes -p <perfil> config set plugins.auto_apply true` deixa a checagem agendada do gateway aplicar as novas versões revisadas do catálogo e puxar as instalações pelo git nesse perfil, e o Hermes Desktop e o dashboard têm o botão "Atualizar todos". O que uma atualização sem ninguém por perto não aceita (capacidades novas, uma fixação do catálogo ampliada) espera você rodar a atualização.

O `plugins update` pela linha de comando só troca os arquivos: não recarrega um gateway que já está rodando, então reinicie o gateway uma vez no fim. O dashboard mostra a aba **Evolution** quando relê os plugins; se ela não aparecer, reinicie o dashboard. (O Hermes recarrega um gateway em execução ao instalar, ligar e desligar, e quando a atualização é aplicada pelo Hermes Desktop, pelo dashboard ou por `/plugins update` num chat.) Se algo ainda estiver rodando (um turno, uma tarefa agendada do Hermes (cron), uma tarefa delegada), o reinício lista e pergunta `Interrupt them and restart the gateway? [y/N]`: responda N e rode de novo em alguns minutos, ou y para interromper. O `status` aponta um gateway que ainda roda código antigo.

O Hermes mantém a fixação quando um plugin é instalado de novo sem `--ref` (ele reinstala o commit fixado), e o `plugins update` dele recusa uma cópia fixada. Por isso o `hermes evolution update` move a fixação com `hermes -p <perfil> plugins install <url> --force --ref <commit>`, o jeito do próprio Hermes, para o commit que o perfil em que você o roda já roda; o perfil continua fixado, agora nesse código.

A maior parte das configurações vale em até um minuto. Algumas esperam o `hermes gateway restart` (no terminal: a próxima sessão): `runtime_guard`, `tool_hints`, e `agent_tools` ou `reflector` quando você o liga (desligar qualquer um dos dois vale sem reiniciar).

Se um perfil ainda tem o plugin com o nome antigo `hermes-evolution`, rode `hermes -p <perfil> plugins remove hermes-evolution` e depois `hermes -p <perfil> plugins install hermes-atlas-evolution` e `hermes -p <perfil> plugins enable hermes-atlas-evolution` (ou, pelo GitHub, `hermes -p <perfil> plugins install sesantanajr/hermes-atlas-evolution --enable`).

O que mudou em cada versão: [Mudanças](../../CHANGELOG.pt-BR.md).

## Atualizar para a 0.14.0

Atualize todos os perfis como acima e **reinicie o gateway uma vez**: um gateway iniciado por um lançador próprio (que
roda o `gateway.run` como módulo principal) passa a ser reconhecido nesta versão e começa a assumir as rodadas de
aprendizado de todos os perfis que atende. Nada para migrar à mão.

- **As primeiras rodadas depois do reinício** repassam até 14 dias de histórico nos perfis que nunca tiveram uma, um
  perfil por vez, com prioridade de CPU menor, começando de 2 a 7 minutos depois que o gateway sobe.
- **Os veredictos das lições são julgados de novo.** Um "eficaz" agora precisa de evidência; o que foi julgado com
  poucas chamadas é retirado, e uma lição cuja falha continua enquanto o agente a lê passa a ineficaz (aposentada no
  Modo Auto).
- **Todos os perfis de uma vez**: `hermes evolution fleet` mostra todos; `hermes evolution fleet auto on` liga o Modo
  Auto em todos pelo `hermes config set` e guarda os valores de antes de cada perfil para o `fleet auto off`;
  `hermes evolution fleet schedule` agenda o relatório diário como tarefa sem agente.
- **Vigiar o pino do gerador de uma sobreposição** (opcional): se um script gera um arquivo carregado no lugar de um do
  Hermes e fixa o sha256 dele, nomeie-o uma vez no perfil que cuida dos outros:

  ```sh
  hermes -p default config set plugins.entries.hermes-atlas-evolution.settings.overlay_pins \
    '["/caminho/build_overlay.py|docs/como-refazer.md"]'
  ```

  Depois de cada atualização do Hermes o plugin avisa quando o arquivo do Hermes não bate mais com o pino. Ele nunca o
  refaz.

## Atualizar para a 0.13.0

Atualize como acima e reinicie o gateway uma vez. Não há nada para migrar à mão; veja o primeiro ponto se você contava com o Modo Auto.

- **O Assistido agora é o modo padrão.** Um perfil sem `mode` definido passa do Auto para o Assistido: sozinho, ele então só tira uma entrada da memória repetida palavra por palavra entre `MEMORY.md` e `USER.md`, e o resto vira proposta que espera você. Um perfil em que você definiu o `mode` continua com ele. Para manter o Modo Auto num perfil, no seu próprio terminal:

  ```sh
  hermes -p <perfil> config set plugins.entries.hermes-atlas-evolution.settings.mode auto
  ```

- **O Modo Auto não sincroniza mais as cópias entre os perfis.** Ele nunca leva a cópia do plugin em outro perfil para um commit. Continua instalando o plugin onde um perfil em uso nunca o teve, mas sozinho só pelo nome no catálogo, quando o perfil que cuida de todos veio do catálogo (o Hermes instala a versão revisada); uma instalação pelo repositório espera a sua tecla (a proposta, ou o `evolution update --install-missing`).
- **O `evolution update` roda o comando do Hermes, e nada mais.** Em cada perfil escolhido ele roda o `plugins update hermes-atlas-evolution` (sem `--force`), só passa uma cópia fixada com `--ref` para o commit do perfil em que você o roda, e instala uma que falta (com `--install-missing`) com o código que esse perfil roda. Ele não consulta mais o último commit do repositório.
- **`install.sh --catalog`.** Com `--install-missing`, instala pelo nome no catálogo em vez do GitHub. O instalador só roda comandos do próprio Hermes e deixa uma cópia fixada como está, imprimindo o comando que a move.
- **Sem rede própria.** O plugin não fala mais com o GitHub nem com nada; os downloads só acontecem dentro do `plugins install` e do `plugins update` do próprio Hermes.
- **As aprovações do Hermes em todo processo filho.** Os comandos que o plugin inicia não herdam mais `HERMES_YOLO_MODE`, `HERMES_YOLO` nem `HERMES_ACP_AUTO_APPROVE`.
- **Correções de estabilidade.** A Proteção do Hermes, o banco de dados, a saída dos comandos que ele roda e o prazo da rodada de aprendizado: veja as [Mudanças](../../CHANGELOG.pt-BR.md).

## Atualizar para a 0.12.0

Atualize como acima e reinicie o gateway uma vez. Não há nada para migrar à mão.

- **No Hermes 0.23.2, atualize já.** O lançador novo dele faz a 0.11.0 e as anteriores recusarem os seus comandos de dono no seu próprio terminal ("Só o operador decide sobre lições e propostas…" em `owner`, `care doctor`, aprovar lições e no próprio `evolution update`). A 0.12.0 o reconhece. Para chegar nela, atualize com o comando do próprio Hermes em cada perfil que tem o plugin (`hermes -p <perfil> plugins update hermes-atlas-evolution`) ou com `sh install.sh --all --yes --restart` a partir de um clone novo, e reinicie o gateway. Até atualizar, a aba Evolution do dashboard com login continua decidindo.
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
