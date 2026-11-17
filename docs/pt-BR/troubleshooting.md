# Solução de problemas

[English](../en-US/troubleshooting.md) · **Português (Brasil)** · [Início da documentação](README.md)

Cada item: o que você vê, por quê, e o que fazer. O `hermes evolution status` é sempre o primeiro olhar: ele lista o que precisa de atenção com o comando que resolve.

## O gateway não volta depois que o Hermes se atualizou

**Você vê** `status=203/EXEC` em `systemctl --user status hermes-gateway`, reinício em loop, ou `hermes gateway restart` esperando por minutos.

**Por quê** o Hermes trocou o Python embutido e o serviço do gateway no systemd ainda chama o antigo.

**Faça**

```bash
hermes evolution care fix-gateway     # aponta o serviço para o Python que o Hermes usa agora (guarda uma cópia do arquivo)
hermes evolution care undo-gateway    # põe o arquivo de volta, se um dia precisar
```

No Modo Auto o plugin repara isso sozinho (no máximo uma vez por dia, só o serviço de usuário do próprio perfil, nunca com um gateway rodando) e registra no histórico. Enquanto um serviço ainda inicia de uma pasta antiga de ferramentas, o `hermes pm gc` fica segurado para nunca apagar esse Python.

## O `hermes plugins update` diz "is pinned to"

**Por quê** a cópia daquele perfil foi instalada num commit fixo, e o Hermes mantém a fixação em toda reinstalação.

**Faça** `hermes evolution update --profiles <perfil>` (ou `--all`): ele move a fixação para o último commit com o próprio `plugins install <url> --force --ref <commit>` do Hermes. No Modo Auto, o perfil que cuida do Hermes inteiro mantém cada cópia no commit dele.

## `hermes evolution` não é encontrado, ou um perfil mostra "nenhum evento hoje"

**Por quê** o plugin não está instalado ou não está ligado naquele perfil (o Hermes carrega plugins por perfil), ou o gateway ainda roda o código carregado antes da instalação.

**Faça**

```bash
hermes -p <perfil> plugins list                                              # o hermes-atlas-evolution está lá e ligado?
hermes -p <perfil> plugins install sesantanajr/hermes-atlas-evolution --enable
hermes gateway restart
```

## O `status` diz que um gateway em execução roda código antigo

**Por quê** o `hermes plugins update` troca os arquivos mas não recarrega um gateway em execução.

**Faça** `hermes gateway restart` uma vez. Ele lista os turnos ainda em andamento e pergunta antes de interrompê-los.

## O Índice de evolução diz "ainda não"

**Por quê** ele precisa de pelo menos duas partes com dados suficientes nos últimos 7 dias, por exemplo 40 chamadas de ferramentas e 20 turnos interativos.

**Faça** nada: ele aparece sozinho depois de alguns dias de uso normal. O `hermes evolution status --format json` mostra a amostra por trás de cada parte.

## O `status` diz que o plugin está somando tempo a uma chamada

**Por quê** a cauda lenta (p95) de um dos ganchos dele passou de 50 ms em pelo menos 200 chamadas; um disco lento ou um banco que outro processo mantém ocupado é a causa comum.

**Faça** veja o `hermes evolution care check` (disco, o banco de sessões, memória). Se o disco é uma pasta de rede ou sincronizada (OneDrive, NFS), mova o `HERMES_HOME` para um disco local.

## O `status` diz que o banco do plugin estava danificado e foi recomeçado

**Por quê** o arquivo foi corrompido (queda de energia, disco cheio, uma ferramenta de sincronização copiando no meio da gravação). O plugin confirmou o dano com a checagem do próprio SQLite, pôs o arquivo de lado como `evolution/evolution.sqlite3.corrupt-<hora>` e começou um novo; o próprio Hermes não foi afetado.

**Faça** nada. As duas cópias danificadas mais novas ficam guardadas; apague quando não precisar mais delas.

## A Proteção do Hermes barrou um comando

**Por quê** o comando do agente reiniciaria ou mataria o gateway, apagaria aquilo em que o Hermes roda, ou mudaria este plugin.

**Faça** rode o comando você mesmo, no seu próprio terminal, se era isso que queria. Para desligar a proteção num perfil (não recomendado): `hermes config set plugins.entries.hermes-atlas-evolution.settings.runtime_guard false` e depois `hermes gateway restart`.

## Um comando responde "Another learning pass is running in this profile"

**Por quê** as rodadas nunca rodam duas ao mesmo tempo num perfil (a rodada em segundo plano do gateway está com ela).

**Faça** tente de novo em um minuto.

## Um comando se recusa a rodar no "terminal de um agente"

**Por quê** decisões (aprovar lições ou propostas, desfazer, configurações) são do dono: elas se recusam a rodar num shell que uma sessão de agente abriu, mesmo com o ambiente limpo.

**Faça** rode no seu próprio terminal, ou decida no dashboard com login.

## Uma configuração que mudei não valeu

**Por quê** a maioria vale em até um minuto; `runtime_guard`, `tool_hints` e ligar `agent_tools` decidem quais ganchos o Hermes chama, então valem no próximo carregamento.

**Faça** `hermes gateway restart` (no terminal, a próxima sessão já pega). O `status` lista as configurações esperando por isso.

## Remover o plugin por completo

```bash
hermes -p <perfil> plugins remove hermes-atlas-evolution    # em cada perfil
```

Depois apague `<pasta do perfil>/evolution/` em cada perfil para remover os dados (lições, histórico, contadores).

**Ainda com problema?** Abra uma issue com a saída de `hermes evolution status --format json` (ela não tem texto de conversa): [issues](https://github.com/sesantanajr/hermes-atlas-evolution/issues).
