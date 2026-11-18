# Solução de problemas

[English](../en-US/troubleshooting.md) · **Português (Brasil)** · [Início da documentação](README.md)

Cada item: o que você vê, por quê, e o que fazer. O `hermes -p <perfil> evolution status` é sempre o primeiro olhar: ele lista o que pede atenção com o comando que resolve. `<perfil>` é o nome do perfil, `default` para o perfil raiz.

## O gateway não volta depois que o Hermes se atualizou

**Você vê** `status=203/EXEC` em `systemctl --user status hermes-gateway`, reinício em loop, ou `hermes gateway restart` esperando por minutos. Ou o `status` diz que o gateway está rodando mas o programa que o serviço dele inicia foi apagado: ele roda agora, e o próximo reinício falha.

**Por quê** o Hermes trocou o Python embutido e o serviço do gateway no systemd ainda chama o antigo.

**Faça**

```bash
hermes -p <perfil> evolution care fix-gateway     # aponta o serviço para o Python que o Hermes usa agora (guarda uma cópia do arquivo)
hermes -p <perfil> evolution care undo-gateway    # põe o arquivo de volta, se um dia precisar
```

O gateway só é reparado (pelo Modo Auto ou por `care fix-gateway`) quando nenhum gateway deste Hermes está rodando, inclusive os degradados e os que estão subindo, e nenhum serviço de gateway de qualquer escopo roda, sobe ou para, e só o serviço que o Hermes dá a esta pasta e que roda este `HERMES_HOME`. A única exceção é o próprio serviço reparado: quando é ele que está rodando (o programa dele foi apagado por baixo), o `care fix-gateway` corrige o arquivo e iniciá-lo não muda nada; o Modo Auto nunca mexe num serviço que está rodando. O `hermes pm gc` apaga toda pasta de ferramentas que o `facts.json` não cita; qualquer pasta usada por um serviço ou programa em execução o bloqueia. O `care undo-gateway` funciona de qualquer perfil, desfaz um reparo por vez até o arquivo original, e um reparo desfeito nunca é repetido sozinho.

No Modo Auto o plugin repara isso sozinho, no máximo uma vez por dia, só um serviço de usuário, e registra no histórico. Quando o `care fix-gateway` diz "Agora não", ele diz o que está no caminho (um gateway rodando, um serviço subindo ou parando, um escopo que não pôde ler, um serviço de outro Hermes); nada foi mudado. "Ainda subindo" quer dizer que o systemd está iniciando o gateway: confira de novo em um minuto.

## O `hermes plugins update` diz "is pinned to"

**Por quê** a cópia daquele perfil foi instalada num commit fixo, e o Hermes mantém a fixação em toda reinstalação.

**Faça** `hermes -p <líder> evolution update --profiles <perfil>`, rodado a partir do perfil cujo código você quer ali (`<líder>`): ele move a fixação para o commit que esse perfil roda, com o próprio `plugins install <url> --force --ref <commit>` do Hermes, e só quando as duas cópias vêm deste repositório. Para escolher outro commit você mesmo, rode esse `plugins install` com o commit que quiser. O plugin nunca escolhe um commit e nunca move uma fixação sozinho, em nenhum modo.

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

**Faça** nada: ele aparece sozinho depois de alguns dias de uso normal. O `hermes -p <perfil> evolution status --format json` mostra a amostra por trás de cada parte.

## O `status` diz que o plugin está somando tempo a uma chamada

**Por quê** a cauda lenta (p95) de um dos ganchos dele passou de 50 ms em pelo menos 200 chamadas; um disco lento ou um banco que outro processo mantém ocupado é a causa comum.

**Faça** veja o `hermes -p <perfil> evolution care check` (disco, o banco de sessões, memória). Se o disco é uma pasta de rede ou sincronizada (OneDrive, NFS), mova o `HERMES_HOME` para um disco local.

## O `status` diz que o banco do plugin estava danificado e foi recomeçado

**Por quê** o arquivo foi corrompido (queda de energia, disco cheio, uma ferramenta de sincronização copiando no meio da gravação). O plugin confirmou o dano com a checagem do próprio SQLite, pôs o arquivo de lado como `evolution/evolution.sqlite3.corrupt-<hora>` e começou um novo; o próprio Hermes não foi afetado.

**Faça** nada. As duas cópias danificadas mais novas ficam guardadas; apague quando não precisar mais delas.

## A Proteção do Hermes barrou um comando

**Por quê** o comando do agente reiniciaria ou mataria o gateway, apagaria aquilo em que o Hermes roda, ou mudaria este plugin.

**Faça** rode o comando você mesmo, no seu próprio terminal, se era isso que queria. Para desligar a proteção num perfil (não recomendado): `hermes -p <perfil> config set plugins.entries.hermes-atlas-evolution.settings.runtime_guard false` e depois `hermes gateway restart`.

## Um comando responde "Another learning pass is running in this profile"

**Por quê** as rodadas nunca rodam duas ao mesmo tempo num perfil (a rodada em segundo plano do gateway está com ela).

**Faça** tente de novo em um minuto.

## Um comando se recusa a rodar no "terminal de um agente"

**Por quê** decisões (aprovar lições ou propostas, desfazer, configurações) são do dono: elas se recusam a rodar num shell que uma sessão de agente abriu, mesmo com o ambiente limpo.

**Faça** rode no seu próprio terminal, ou decida no dashboard com login.

## Uma configuração que mudei não valeu

**Por quê** a maioria vale em até um minuto; `runtime_guard`, `tool_hints`, e ligar `agent_tools` ou `reflector`, decidem o que o plugin registra no Hermes, então valem no próximo carregamento. Ou o valor está no `settings.json` e ampliaria a confiança: `owner_sender_ids`, `owner_platforms`, `agent_tools` ligado, `tool_hints` ligado, `reflector_daily_limit` acima de 6, `runtime_guard` desligado e um `mode` diferente de `manual` só valem pela configuração do Hermes, porque as ferramentas de arquivo do agente podem gravar o `settings.json`.

**Faça** `hermes gateway restart` (no terminal, a próxima sessão já pega). O `status` lista as configurações esperando por isso, e as chaves do `settings.json` que ignorou, em Pede atenção. Defina essas com `hermes -p <perfil> config set plugins.entries.hermes-atlas-evolution.settings.<chave> <valor>` no seu próprio terminal.

## O `status` diz que o `config.yaml` não foi lido

**Por quê** o plugin não conseguiu ler o `config.yaml` do perfil: o executor avulso sem PyYAML, um arquivo acima de 1 MiB, ou um erro de YAML. Sem ele o plugin não sabe o que você escolheu, então age como no modo Manual: nada muda sozinho, e os valores mostrados são os padrões.

**Faça** rode o comando pelo Hermes (`hermes -p <perfil> evolution …`), ou instale o PyYAML para o executor avulso, ou corrija o arquivo (o `hermes -p <perfil> config check` mostra o que o Hermes acha dele).

## O gateway aparece como "rodando (degradado…)"

**Por quê** o Hermes subiu o gateway e nenhuma plataforma de chat está conectada ainda; as tarefas agendadas seguem rodando. O plugin conta esse gateway como rodando: nunca repara nem sobe um gateway enquanto um assim está no ar.

**Faça** confira as credenciais da plataforma com `hermes -p <perfil> gateway status`. Nada a fazer do lado do plugin.

## O painel mostra "(texto oculto: leia no seu terminal)"

**Por quê** o dashboard não tem login, então qualquer um que carregue a página, inclusive o terminal do agente, poderia lê-lo. O painel então deixa de fora o texto das lições escritas por você, pelo refletor ou pelo agente, como o terminal faz no shell de um agente. As lições de modelo fixo continuam.

**Faça** leia-as com `hermes -p <perfil> evolution lessons list` no seu próprio terminal, ou ligue o login do dashboard.

## Remover o plugin por completo

```bash
hermes -p <perfil> plugins remove hermes-atlas-evolution    # em cada perfil
```

Depois apague `<pasta do perfil>/evolution/` em cada perfil para remover os dados (lições, histórico, contadores).

Para pará-lo sem remover, o `hermes -p <perfil> plugins disable hermes-atlas-evolution` faz um gateway em execução descarregar o plugin; depois de uma remoção, rode `hermes gateway restart`. Nenhum dos dois comandos apaga a pasta de dados. No Modo Auto, o perfil que cuida do Hermes inteiro lembra onde o plugin já esteve e nunca o instala lá de novo sozinho.

**Ainda com problema?** Abra uma issue com a saída de `hermes -p <perfil> evolution status --format json` (ela não tem texto de conversa): [issues](https://github.com/sesantanajr/hermes-atlas-evolution/issues).
