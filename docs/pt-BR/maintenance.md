# Manutenção: a saúde do Hermes e o sistema operacional

[English](../maintenance.md)

Duas telas do menu (`hermes evolution`, telas 12 e 13), a aba **Saúde** do painel no dashboard e dois comandos, `hermes evolution care` e `hermes evolution system`. Nenhum usa o agente; nenhum gasta token, a não ser onde está dito.

## A saúde do Hermes

A passada de aprendizado a lê no máximo a cada 6 horas, e o menu quando você pede, a partir do que o Hermes já grava, sem rede, sem modelo e sem nenhum comando `hermes`:

| Leitura | De onde | Pede atenção quando |
|---|---|---|
| O gateway | `gateway_state.json`, `state/gateway.heartbeat` | diz que está rodando, mas o sinal de vida tem mais de 3 minutos (travado); a memória dele passa do dobro da mediana da última semana e de 512 MB |
| O banco de sessões | `state.db` (só leitura), `state.db-wal` | não pode ser lido; o `PRAGMA quick_check` (uma vez por semana) falha; o log de escrita passa de 64 MB |
| O disco onde o Hermes grava | `statvfs` da pasta do perfil | menos de 256 MB livres (o mínimo do próprio Hermes) ou de 1 GB; no ritmo da última semana ele enche em menos de 14 dias |
| Cada pasta do perfil | `sessions`, `logs`, `checkpoints`, `cache`, `tools`, `backups`, `skills`, `memories`… (no máximo 8 segundos de varredura) | cresceu 1 GB e metade em cerca de uma semana |
| A máquina | `/proc/meminfo`, a carga média | menos de 10% da memória livre; swap mais de 80% usado; carga acima do dobro de CPUs nos últimos 15 minutos |
| As ferramentas do Hermes | `tools/facts.json` | versões de ferramentas gerenciadas pelo Hermes que nada referencia ocupam 300 MB ou mais (só contam nomes que o Hermes gerencia) |
| Depois de um reinício | systemd | o gateway roda como serviço do usuário sem linger: ele não voltaria |

O que conserta é do dono, pelo menu (tela 12) ou por `hermes evolution care <ação> --yes`, cada um com o próprio comando do Hermes:

| Ação | Comando | Notas |
|---|---|---|
| Integridade da instalação | `hermes pm doctor` | sem rede: confere cada arquivo gerenciado pelo Hermes contra a impressão digital registrada; roda antes da manutenção de partida do próprio Hermes |
| Checagem completa do Hermes | `hermes doctor` | consulta as APIs dos provedores configurados (e manda um ping de 1 token para um endpoint que termina em `/anthropic`); sem agente |
| Reparar com o Hermes | `hermes backup --quick --label atlas-evolution` e depois `hermes doctor --fix` | o snapshot vem antes; nada roda se ele falhar; o Hermes o restaura com `/snapshot restore` |
| Liberar versões antigas de ferramentas | `hermes pm gc` | remove do repositório o que nada referencia |

## O sistema operacional

Linux com apt (Ubuntu, Debian). Qualquer outro sistema aparece como "não suportado" e nada roda.

**Checar** (só leitura; diariamente na passada de aprendizado e a cada visita à tela 13): atualizações esperando (as de segurança contadas à parte), um reinício que o sistema espera e os pacotes que o pediram, `dpkg --audit` (uma atualização que parou no meio), `apt-get check` sem a trava do apt (nunca faz o `unattended-upgrades` esperar), outro gerenciador de pacotes rodando agora (o auxiliar `unattended-upgrade-shutdown`, que só espera o desligamento, não conta), pacotes retidos, espaço em `/`, `/boot` e `/var`, atualizações automáticas de segurança, a idade das listas de pacotes, as unidades systemd do Hermes e o linger de que uma unidade de usuário precisa para subir no boot. Fica pronto para atualizar quando nada está pela metade, quebrado ou ocupado e há espaço.

**Planejar**: `apt-get update` e uma simulação do `apt-get --with-new-pkgs upgrade` (ou `full-upgrade` com `--full`): o que seria atualizado, instalado, removido ou retido, e os pacotes que merecem uma palavra: reinicia os containers (docker, containerd), pede reinício (libc6, systemd, o kernel, openssl), reinicia o servidor web, acesso remoto (ssh, sudo), o carregador de boot, o próprio gerenciador de pacotes.

**Atualizar** (root, no seu próprio terminal; nunca o agente, nunca sozinho):

1. A checagem precisa passar.
2. Uma cópia de todos os pacotes instalados com as versões e do `/etc`, só do dono, em `<perfil>/evolution/system/<hora>/`.
3. `apt-get -y --with-new-pkgs upgrade` numa unidade systemd própria (`hermes-evolution-os-upgrade-<hora>`), para que um terminal fechado, uma conexão SSH caída ou um gateway reiniciado nunca deixem o dpkg pela metade. Mantém cada arquivo de configuração que você mudou (`--force-confold`), não reinicia serviço nenhum sozinho (`NEEDRESTART_MODE=l`) e retém os pacotes que você pediu para deixar de fora só nesta vez.
4. Depois: dpkg e apt checados de novo, cada serviço que rodava checado de novo (todos, não só os do Hermes), `hermes --version`, e um relatório com o que mudou e o comando que volta cada um à versão antiga (`apt-get install <pacote>=<versão>`, enquanto essa versão estiver disponível).

O plugin nunca reinicia a máquina. Quando o sistema espera um reinício, planeje-o: os serviços do Hermes voltam sozinhos quando estão habilitados (e, para uma unidade de usuário, com linger).

**Reparar**: quando uma atualização parou no meio (queda de energia, processo morto), `dpkg --configure -a` e depois `apt-get -f install`, mantendo os seus arquivos de configuração; recusado enquanto outro gerenciador de pacotes roda.

### Como foi testado

Em containers (Docker) com Ubuntu 26.04, 24.04 e 22.04 e Debian 13, separados de qualquer sistema de produção:

- uma atualização completa de imagens do Ubuntu 24.04 e 22.04 de abril de 2024 (67 e 55 pacotes, entre eles libc6, apt, dpkg e openssl), com um pacote deixado de fora (mantido na versão antiga, a retenção liberada depois) e um arquivo de configuração editado do libc6 (mantido);
- uma queda de energia no meio da atualização (o container morto enquanto o dpkg desempacotava libc6, zlib ou libsystemd, e depois ligado de novo): a checagem encontra o dpkg pela metade e recusa uma atualização, o reparo recusa enquanto outro apt roda, depois termina o trabalho, e a checagem volta a ficar pronta;
- uma atualização do Ubuntu 26.04 com o Hermes 0.23.1 instalado, pelo `hermes evolution system upgrade`: libc6 2.43 atualizado, o Hermes responde depois, nenhum serviço parou.

## O resumo semanal

Decisões que esperam precisam de algo que as lembre (o Hermes 0.23.1 descarta os avisos de fim de sessão). A sugestão do menu, ou `hermes evolution schedule-digest --deliver <local|canal>`, agenda o resumo semanal (segundas às 08:00) como um job de cron do Hermes que roda um script, não o agente (`--no-agent`): não gasta token. Uma vez só; desfeito pelo histórico (`hermes cron remove`).
