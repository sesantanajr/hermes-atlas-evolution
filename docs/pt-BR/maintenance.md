# Manutenção: a saúde do Hermes e o sistema operacional

[English](../en-US/maintenance.md) | **Português (Brasil)**

Onde olhar: as telas 12 (Saúde do Hermes), 13 (Sistema operacional) e 14 (Ambiente) do menu (`hermes evolution`), as abas **Saúde** e **Ambiente** do painel no dashboard, e os comandos `hermes evolution care` e `hermes evolution system`. Nenhum deles usa o agente, e nenhum gasta token.

## A saúde do Hermes

### O que o plugin lê

A rodada de aprendizado lê isto no máximo a cada 6 horas, e a tela 12 lê de novo cada vez que você a abre. Tudo vem do que o Hermes já grava: sem rede, sem modelo, sem nenhum comando `hermes`.

| Leitura | De onde | Pede atenção quando |
|---|---|---|
| O gateway | `gateway_state.json`, `state/gateway.heartbeat` | diz que está rodando, mas o sinal de vida tem mais de 3 minutos (travado); sem sinal de vida recente, o processo sumiu ou o pid agora é de outro programa (caiu; checado só em sistemas POSIX; um sinal de vida recente conta como rodando, mesmo de um gateway em outro namespace de PID); a memória dele passa do dobro da mediana da última semana e de 512 MB |
| O banco de sessões | `state.db` (só leitura), `state.db-wal` | não pode ser lido; o `PRAGMA quick_check` (uma vez por semana) falha; o log de escrita passa de 64 MB |
| O disco onde o Hermes grava | `statvfs` da pasta do perfil | menos de 256 MB livres (o mínimo do próprio Hermes) ou menos de 1 GB; no ritmo da última semana ele enche em menos de 14 dias |
| Cada pasta do perfil | `sessions`, `logs`, `checkpoints`, `cache`, `tools`, `backups`, `skills`, `memories`… (no máximo 8 segundos de varredura) | cresceu 1 GB e metade em cerca de uma semana |
| A máquina | `/proc/meminfo`, a carga média | menos de 10% da memória livre; swap mais de 80% usado; carga acima do dobro de CPUs nos últimos 15 minutos |
| As ferramentas do Hermes | `tools/facts.json` na raiz do Hermes (um repositório só para todos os perfis) | versões de ferramentas gerenciadas pelo Hermes que nada referencia ocupam 300 MB ou mais (só contam nomes que o Hermes gerencia) |
| Depois de um reinício | systemd | o gateway roda como serviço do usuário sem linger: ele não voltaria. Correção: `loginctl enable-linger <usuário>` |

O que pede atenção aparece na tela 12, na aba Saúde do painel e no `hermes evolution status`, cada item com o comando que resolve.

### O doctor do próprio Hermes, uma vez por dia

O `hermes doctor` é a checagem completa de uma instalação do Hermes. O plugin roda essas checagens por você, sem as partes que usam a rede ou saem da máquina, e transforma o que elas encontram em itens com o que fazer.

- **Quem roda.** O perfil que cuida do Hermes inteiro (o líder, veja [O ambiente](evolution-loop.md#o-ambiente)), na rodada de aprendizado dele, uma vez por dia para cada perfil em uso: um perfil com sessões nos últimos 7 dias, um perfil com o plugin ligado e o perfil padrão. Um perfil que ninguém usa espera você pedir. Uma rodada cobre tantos perfis quantos couberem no tempo dela, e só enquanto a máquina tem pelo menos 600 MB de memória livre (uma checagem usa cerca de 210 MB por alguns segundos); os outros esperam a próxima rodada.
- **Como.** Num processo curto do próprio Python do Hermes, num grupo de processos só dele, com a pasta daquele perfil e um ambiente limpo (só passam `PATH`, `HOME`, o idioma do sistema e algumas variáveis do sistema). Cada checagem é chamada como o `hermes doctor` a chama, sem `--fix`; um erro numa checagem, inclusive as saídas do próprio Hermes, é contido ali, então nunca esconde as outras. Como o `hermes doctor`, a checagem carrega os plugins do perfil para listar as ferramentas deles. Cinco checagens ficam de fora: os testes das APIs dos provedores, o `npm audit`, a importação do dashboard, o Skills Hub (usam a rede ou levam minutos) e o backend do terminal (pode abrir uma conexão SSH ou subir um container). Uma checagem leva cerca de 7 segundos por perfil no Hermes 0.23.1. Aos 120 segundos, o grupo de processos inteiro é parado, então nada que ela tenha iniciado fica para trás.
- **O que muda.** Nenhuma configuração e nenhum arquivo seu. A marca que deixa é a mesma do `hermes doctor`: os logs e os arquivos de trava do próprio Hermes.
- **Por quanto tempo um resultado vale.** Um resultado com mais de 3 dias deixa de aparecer: o perfil ficou parado, ou as checagens foram puladas. Quando uma mudança se mostra desatualizada no disco, o resultado guardado daquele perfil é descartado e as checagens rodam de novo na próxima olhada.
- **Olhar de novo agora.** `v` na tela 14, ou `hermes evolution environment scan`, roda de novo para todos os perfis em uso.

O que o doctor aponta vira itens do Ambiente (tela 14 e a aba Ambiente do painel). Cada item diz o que foi visto, o que fazer, com comandos `hermes -p <perfil> ...`, e cita as palavras do próprio Hermes:

| Item | Aparece quando | O que você faz |
|---|---|---|
| Problema de segurança (risco alto, primeiro da lista) | o doctor cita um alerta de segurança (o item é o id do alerta, como `security.GHSA-...`) ou um servidor MCP suspeito (`mcp.<nome>`) | Aja já, seguindo o texto do Hermes: desinstale o pacote ou remova o servidor MCP citado, e troque todas as credenciais que ele pode ter visto. Para um servidor MCP, o comando é `hermes -p <perfil> mcp remove <nome>`; para um alerta, `hermes -p <perfil> doctor --ack <id>` fecha o alerta depois que você resolveu. |
| Um cache de pacotes que o Hermes nunca limpa | uma pasta em `cache/` que se refaz sozinha quando precisa (`uv`, `pnpm`, `npm`, `pip`, `yarn`, `huggingface`, `piper-voices`) está grande e fora de toda limpeza (o Hermes só limpa `cache/scratch` e `cache/terminal`) | Aplique: uma tecla a move para a lixeira do plugin (`evolution/trash/<perfil>/` do líder). Uma pasta por vez, nunca no **Aplicar tudo**. **Desfazer** a devolve; se a pasta já existir de novo, o desfazer avisa e a cópia fica na lixeira até ser apagada. |
| Qualquer outra pasta de cache grande | outra pasta em `cache/`: `browser` (o perfil do navegador do agente, com logins e cookies), `documents` (arquivos anexados às conversas, que sessões antigas usam), `images`, `generated`, `audio`, `delegation`, `spillover`, `web`… | É sua: o plugin nunca a move, porque o Hermes ou as sessões ainda a usam. O item diz o que a pasta guarda e dá o comando para olhar dentro, `du -sh <caminho>/* \| sort -h \| tail -20`; apague só o que não precisa mais. |
| Pontos de restauração acima do limite do Hermes | os pontos de restauração do `/rollback` passam do limite de tamanho do Hermes | Veja o que ocupa espaço com `hermes -p <perfil> checkpoints status`. O Hermes sempre guarda o ponto mais recente de cada projeto, então o limite dele é um piso. Depois escolha: guardar menos dias (`hermes -p <perfil> config set checkpoints.retention_days 3` e depois `hermes -p <perfil> checkpoints prune`) ou, se você nunca usa o `/rollback`, desligar (`hermes -p <perfil> config set checkpoints.enabled false` e depois `hermes -p <perfil> checkpoints clear`). Pontos apagados não voltam. |
| Um problema que o plugin conhece | ferramentas ligadas sem chave, falta do `.env`, nenhuma chave de provedor, um banco de sessões para enxugar, reparar ou recuperar, linger, `config.yaml` para migrar ou com ajustes no lugar errado, um `HERMES_MAX_ITERATIONS` antigo, o provedor de memória desligado, um plugin desligado por uma atualização ou nas duas listas, tarefas agendadas sendo puladas ou com o disco quase cheio, um gateway por perfil, dois perfis com a mesma credencial de chat, `hermes` fora do `PATH` | O item diz o fato e a ação. Exemplos: um plugin que uma atualização desligou → `hermes -p <perfil> plugins enable <nome>` (ou `plugins remove <nome>`, se você não o quer); ferramentas sem chave → `hermes -p <perfil> setup`, ou `hermes -p <perfil> tools` para desligá-las; linger → `sudo loginctl enable-linger $USER`. A próxima checagem diária fecha o item quando ele estiver resolvido. |
| Qualquer outro problema que o doctor liste | uma frase que o plugin não conhece | Aparece nas palavras do próprio Hermes, com o comando que o Hermes cita. |

Um problema da máquina inteira (um alerta de segurança, linger, o `PATH`, um gateway por perfil, dois perfis com a mesma credencial de chat, o disco das tarefas agendadas) aparece uma vez só, no primeiro perfil que o viu (o líder primeiro), com os outros perfis citados.

Peças opcionais que só não foram configuradas (um pacote opcional, um provedor sem login) não são problemas: são contadas, nunca propostas. A linha de cada perfil na tela 14 mostra quantos problemas, avisos e peças opcionais há, e quando foi checado; quando o doctor não conseguiu rodar (tempo esgotado, um Hermes sem essas checagens), a linha diz isso e o motivo. Nesse caso, rode o `hermes doctor` você mesmo.

Antes de mover um cache de pacotes, o plugin roda de novo, para aquele perfil, a checagem do doctor que o encontrou. Se a situação sumiu, nada é movido e o item fecha.

**A lixeira** é esvaziada a cada rodada de aprendizado, em qualquer modo: o que entrou nela há mais de 30 dias é apagado.

### O que você pode rodar

Cada ação roda o próprio comando do Hermes, pela tela 12 ou com `hermes evolution care <ação> --yes`, no seu próprio terminal (nunca no shell de um agente). Cada uma tem limite de 900 segundos.

| Ação | `care` | Comando | O que faz |
|---|---|---|---|
| Checar de novo | `check` | nenhum | Lê a tabela de saúde de novo agora. Aberta a qualquer um. |
| Integridade da instalação | `pm-doctor` | `hermes pm doctor` | Sem rede. Confere cada arquivo gerenciado pelo Hermes contra a impressão digital registrada. Roda antes da manutenção de partida do próprio Hermes. |
| Checagem completa do Hermes | `doctor` | `hermes doctor` | Chama a API de cada provedor configurado (um endpoint que termina em `/anthropic` recebe um pedido de 1 token), roda o `npm audit` contra o registro do npm, consulta o `gh auth status` e importa o dashboard num subprocesso. Usa a rede e pode levar minutos. Não muda nenhuma configuração. |
| Reparar com o Hermes | `fix` | `hermes backup --quick --label atlas-evolution` e depois `hermes doctor --fix` | A cópia de segurança roda antes. Se ela falhar, nada é reparado. |
| Liberar versões antigas de ferramentas | `gc` | `hermes pm gc` | Remove as versões de ferramentas que nada referencia. Não há o que desfazer: o Hermes baixa uma versão de novo se algo pedir. |

**Como ler a resposta do doctor.** O `hermes doctor` termina com código de saída 1 quando encontrou problemas. Isso é uma resposta, não uma falha: o plugin mostra quantos problemas, as linhas marcadas com ✗ e a própria lista numerada do doctor com o que fazer. Saída 0 quer dizer nada a arrumar.

**O que o `hermes doctor --fix` pode mudar:** criar pastas que faltam e o arquivo `.env`, migrar o `config.yaml` (o Hermes guarda uma cópia antes), reparar o banco de sessões, recriar o lançador `hermes` e baixar o Chromium quando a ferramenta de navegador precisar.

**O que a cópia rápida devolve:** o `config.yaml`, o `.env`, o banco de sessões e as tarefas do cron. Para voltar, rode `/snapshot restore <id>` num chat do Hermes.

**O `hermes pm gc` sozinho.** No Modo Auto, o perfil líder roda o `hermes pm gc` uma vez para o Hermes inteiro (há um único repositório de ferramentas, na raiz) quando 300 MB ou mais de versões antigas estão sem uso, no máximo uma vez por semana. Fica registrado no histórico de decisões.

## O sistema operacional

Linux com apt (Ubuntu, Debian). Qualquer outro sistema aparece como "não suportado" e nada roda.

**Checar** (só leitura; diariamente na rodada de aprendizado e a cada visita à tela 13):

- atualizações esperando, com as de segurança contadas à parte;
- um reinício que o sistema espera, e os pacotes que o pediram;
- `dpkg --audit` (uma atualização que parou no meio) e `apt-get check` sem a trava do apt (nunca faz o `unattended-upgrades` esperar);
- se outro gerenciador de pacotes trabalha agora: uma trava do dpkg ou do apt em uso (lida em `/proc/locks`), ou um processo `apt`, `apt-get`, `aptitude` ou `dpkg`. O auxiliar `unattended-upgrade-shutdown`, que só espera o desligamento, não conta. Enquanto outro gerenciador de pacotes trabalha, um dpkg inacabado é o trabalho dele em andamento e não é relatado; um que trabalha há mais de 6 horas (um `apt` esquecido esperando a resposta) pede atenção;
- pacotes retidos, e os que uma execução interrompida deste plugin ainda retém;
- espaço em `/`, `/boot` e `/var`;
- atualizações automáticas de segurança, lidas com `apt-config dump` (a leitura do próprio apt de todos os arquivos de `apt.conf.d`);
- a idade das listas de pacotes: o arquivo `Release` mais novo que o apt baixou, à mão ou pelo agendamento;
- as unidades systemd do Hermes: uma unidade que roda agora e não está habilitada não voltaria depois de um reinício (uma unidade que você deixou parada não é relatada); uma unidade de usuário do dono do perfil também precisa de linger.

Fica pronto para atualizar quando nada está pela metade, quebrado ou ocupado e há espaço.

**Planejar**: `apt-get update` e uma simulação do `apt-get --with-new-pkgs upgrade` (ou `full-upgrade` com `--full`): o que seria atualizado, instalado, removido ou retido, e os pacotes que merecem uma palavra: reinicia os containers (docker, containerd), pede reinício (libc6, systemd, o kernel, openssl), reinicia o servidor web, acesso remoto (ssh, sudo), o carregador de boot, o próprio gerenciador de pacotes.

**Atualizar** (root, no seu próprio terminal; nunca o agente, nunca sozinho):

1. A checagem precisa passar, e o perfil do Hermes e o código do próprio plugin precisam ser do root. Se outro usuário pode mudá-los, o root rodaria o que esse usuário escreveu: o plugin recusa e o plano mostra o que fazer com o apt você mesmo.
2. Uma cópia de todos os pacotes instalados com as versões e do `/etc`, numa pasta que só o root lê, fora de todo perfil do Hermes (portanto nunca dentro de um backup do Hermes): `/var/lib/hermes-atlas-evolution/system/<hora>-<aleatório>/`. Os arquivos são criados novos, nunca através de um link; as 5 últimas execuções são guardadas.
3. `apt-get -y --with-new-pkgs upgrade` numa unidade systemd própria (`hermes-evolution-os-upgrade-<execução>`), para que um terminal fechado, uma conexão SSH caída ou um gateway reiniciado nunca deixem o dpkg pela metade. Mantém cada arquivo de configuração que você mudou (`--force-confold`), não reinicia serviço nenhum sozinho (`NEEDRESTART_MODE=l`) e retém os pacotes que você pediu para deixar de fora só nesta vez: eles são anotados antes de serem retidos e liberados quando a execução termina, falha ou é parada (`systemctl stop`, Ctrl+C, conexão caída); um pacote só sai dessa lista quando o `apt-mark` realmente o liberou, esperando até 3 minutos por um dpkg que ainda termina o trabalho. Um nome que o apt não aceitaria é recusado, nunca descartado em silêncio.
4. Depois: dpkg e apt checados de novo, cada serviço que rodava checado de novo (todos, não só os do Hermes; um que falhou conta contra a atualização, um que terminou sem erro aparece à parte, já que a maioria para quando fica ociosa), unidades que passaram a falhar, `hermes --version`, e um relatório com o que mudou e o comando que volta cada um à versão antiga (`apt-get install <pacote>=<versão>`, enquanto essa versão estiver disponível). `hermes evolution system report` e o menu mostram o último, escrito na primeira vez em que é pedido se o menu foi fechado.

O plugin nunca reinicia a máquina. Quando o sistema espera um reinício, planeje-o: os serviços do Hermes voltam sozinhos quando estão habilitados (e, para uma unidade de usuário, com linger).

**Uma execução que sumiu** (a máquina perdeu energia, a unidade foi morta): o menu e o `system report` dizem isso em vez de esperar para sempre, e a checagem lista os pacotes que essa execução ainda retém.

**Reparar**, numa unidade systemd própria como a atualização: libera o que uma execução interrompida do plugin ainda retém, depois `dpkg --configure -a` e `apt-get -f install`, mantendo os seus arquivos de configuração; recusado enquanto outro gerenciador de pacotes trabalha.

### Como foi testado

Em containers (Docker) com Ubuntu 26.04, 24.04 e 22.04 e Debian 13, separados de qualquer sistema de produção:

- uma atualização completa de imagens do Ubuntu 24.04 e 22.04 de abril de 2024 (67 e 55 pacotes, entre eles libc6, apt, dpkg e openssl), com um pacote deixado de fora (mantido na versão antiga, a retenção liberada depois) e um arquivo de configuração editado do libc6 (mantido); a pasta da execução só do root, fora do perfil;
- uma queda de energia no meio da atualização (o container morto enquanto o dpkg desempacotava libc6, zlib ou libsystemd, e depois ligado de novo): a checagem encontra o dpkg pela metade e o pacote que a execução ainda retinha, e recusa uma atualização; o reparo recusa enquanto outro apt roda, depois libera a retenção e termina o trabalho, e a checagem volta a ficar pronta;
- a atualização parada como o `systemctl stop` faz (SIGTERM para toda a execução) enquanto o script de um pacote ainda rodava, com o dpkg segurando a trava: a execução espera por ele e libera o pacote deixado de fora, e o reparo termina o trabalho do dpkg;
- um pacote deixado de fora escrito com a arquitetura (`zlib1g:amd64`) quando o dono já o retinha: a retenção do dono fica intacta;
- uma trava real do dpkg segurada por outro programa: a checagem diz ocupado e nomeia a trava, e fica pronta quando ela é solta;
- um Hermes de outro usuário: o root recusa atualizar por meio dele;
- uma atualização do Ubuntu 26.04 com o Hermes 0.23.1 instalado, pelo `hermes evolution system upgrade`: libc6 2.43 atualizado, o Hermes responde depois, nenhum serviço parou.

## O resumo semanal

Decisões que esperam precisam de algo que as lembre (o Hermes 0.23.1 descarta os avisos de fim de sessão). A sugestão do menu, ou `hermes evolution schedule-digest --deliver <local|canal>`, agenda o resumo semanal (segundas às 08:00) como uma tarefa agendada do Hermes (cron) que roda um script, não o agente (`--no-agent`): não gasta token. Ele é criado uma vez só e desfeito pelo histórico de decisões (`hermes cron remove`).
