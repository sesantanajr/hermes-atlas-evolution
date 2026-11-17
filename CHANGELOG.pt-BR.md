# Mudanças

*In English: [CHANGELOG.md](CHANGELOG.md), with every version.*

## 0.9.0

O Atlas Evolution passa a ler o doctor do próprio Hermes para você todo dia, em todos os perfis, e fala o seu idioma em tudo: no painel, no menu, em cada comando e no formulário de configurações. Uma auditoria de todas as ações dele e duas revisões independentes corrigiram o que encontraram.

### Novidades

- **O doctor do Hermes, todo dia** (`doctor.py`). Uma vez por dia, em cada perfil em uso, o perfil que cuida do Hermes inteiro roda as checagens do `hermes doctor` num processo curto do Python do próprio Hermes, com a pasta do perfil e um ambiente limpo, sem `--fix` e sem as cinco checagens que usam a rede, levam minutos ou saem da máquina (sondas dos provedores, npm audit, importação do dashboard, Skills Hub, backend do terminal). Cerca de 7 s e 210 MB por perfil, um de cada vez, e pulado quando a máquina tem menos de 600 MB livres. Nada é mudado: como o `hermes doctor`, ela carrega os plugins do perfil para listar as ferramentas e deixa os mesmos logs e arquivos de trava. Um resultado deixa de valer depois de 3 dias sem outro, e um problema da máquina inteira (alerta de segurança, linger, PATH, supervisão do gateway) aparece uma vez só. O que as checagens encontram entra na tela e na aba Ambiente, o mais importante primeiro:
  - uma pasta de cache que o Hermes nunca limpa, com mais de 1 GB: um cache de pacotes que se refaz sozinho (uv, pnpm, npm, pip...) vai para a lixeira do plugin com uma tecla, um de cada vez (volta pelo desfazer, apagado depois de 30 dias); qualquer outra pasta (logins do navegador, anexos das conversas, imagens geradas) fica com você, com o que ela guarda e o comando para olhar dentro;
  - pontos de restauração acima do limite do Hermes: as três saídas do Hermes (menos dias, limpar ou desligar o /rollback), que você roda;
  - um alerta de segurança ou um servidor MCP suspeito: primeiro da lista, com `hermes doctor --ack <id>` ou `hermes mcp remove <nome>`;
  - qualquer outro problema que o doctor lista, com título e explicação simples quando o plugin o conhece (ferramentas ligadas sem chave, `.env` faltando, nenhuma chave de provedor, banco de sessões para reparar, gateway que não volta depois de reiniciar, config para migrar, provedor de memória ou plugin desligado, credencial de chat repetida...) e, nos outros casos, as palavras e o comando do próprio Hermes.
  Peças opcionais que nunca foram configuradas (um pacote opcional, um provedor sem login) são contadas, não propostas. Antes de uma mudança rodar, a checagem que a encontrou roda de novo naquele perfil.
- **O doctor completo, explicado** (menu → Saúde do Hermes). O código de saída 1 aparece como "o doctor encontrou N problemas", com a própria lista numerada do que fazer; antes do `hermes doctor --fix` o menu diz o que ele pode mudar e o que o `hermes backup --quick` devolve.
- Quantos problemas e avisos o doctor encontrou em cada perfil, no menu (tela 14), na aba Ambiente do painel e no `status`.

### Mudanças

- **Bilíngue em tudo.** O painel segue o idioma do perfil (com `auto`: o idioma escolhido para o perfil no Hermes, depois o do dashboard); `care`, `environment`, `decisions`, `lessons list`, `proposals list`, `learn`, `skills`, `experiments`, `system`, `health`, `usage`, `history`, `tick` e `schedule-digest` imprimem texto legível no idioma do perfil (scripts que leem o JSON deles acrescentam `--format json`; o JSON é o mesmo); a ajuda também sai em português; o formulário de configurações mostra cada rótulo como "English · Português"; códigos como `busy`, `one_by_one` ou `applied` viram frases; datas no horário local com o fuso; tamanhos e números no formato do idioma.
- As ferramentas do agente só são registradas com o `agent_tools` ligado, então o `hermes doctor` não avisa mais "hermes-atlas-evolution (system dependency not met)" em todo Hermes. Ligá-las pede `hermes gateway restart`; desligar vale na hora.
- O painel: "Esperando você" conta cada item uma vez; o comando de uma mudança manual aprovada fica na tela com o botão Copiar e no Histórico; campos numéricos podem ser esvaziados; o Copiar funciona sem a API de área de transferência; a janela de confirmação recebe o foco e fecha com Esc; as abas funcionam com as setas.
- O menu mostra o Markdown como texto de terminal, fica nas Configurações depois de um erro, avisa quando uma tecla não é opção e mostra a lição no seu idioma ao abri-la.
- A skill de revisão não pede mais ao agente o texto de lições que ele não pode ver e sabe que aprovar já trava a skill.
- Uma observação dispensada volta depois de 30 dias só se a situação continuar; uma mudança entre perfis que volta a valer um dia depois de aplicada é dita de novo.
- A documentação descreve o que o plugin faz hoje, em inglês e em português, sem histórico de versões.

### Correções

- A lixeira apagava na hora uma pasta cuja data era de mais de 30 dias atrás (renomear mantém a data), e o desfazer falhava. Agora os 30 dias contam a partir do dia em que ela entra, e a lixeira é esvaziada a cada rodada de aprendizado (antes, só quando outra coisa entrava nela).
- O Modo Auto podia reinstalar o plugin num perfil de onde você o removeu (o `hermes plugins remove` apaga também as configurações dele). O banco do plugin que fica lá agora conta como decisão sua.
- Um plugin instalado como link para uma pasta (um checkout de desenvolvimento) contava como ausente: o ambiente nunca rodava e a instalação era tentada todo dia.
- Um hook de transformação que devolve o resultado original (`return result`) contava como não mudando nada, mas o Hermes fica com o primeiro texto que qualquer callback devolve: uma sobreposição real passava.
- A instalação fixava o commit do perfil que cuida do Hermes inteiro mesmo vindo de um fork ou de um commit local, que o Hermes não consegue baixar. Agora só fixa quando o checkout é do repositório publicado.
- Os nomes de perfil seguem a regra do próprio Hermes, então uma cópia como `profiles/work.bak` não é lida como perfil.
- Uma mudança que já não valia no disco era reaberta pela olhada em cache na rodada seguinte.
- O `hermes pm gc` olhava um depósito de ferramentas por perfil (há um só, na raiz) e só rodava do perfil padrão; agora roda uma vez para o Hermes inteiro, do perfil que cuida dele, marcado antes de começar.
- A rodada em segundo plano não tinha prazo dentro da reserva de 15 minutos.
- A proteção do Hermes deixava passar `kill $(pgrep -f 'gateway run')`, `pgrep -f gateway | xargs kill` e buscas com awk: uma busca de pid que seleciona o gateway agora conta (`pgrep` contra a linha de comando com `-f` e contra o nome sem ele, `grep`/`awk` só numa lista de processos vinda de um pipe), e comandos comuns como `kill $(cat app.pid)` ou `kill $(pgrep -u root worker)` continuam passando.
- O `hermes doctor` que termina com 1 porque encontrou problemas aparecia como falha, e a lista do que fazer se perdia.
- O banco de sessões era aberto por uma URI sem escape (um caminho com `#` abria outro arquivo, com escrita).
- No Windows, checar o processo do gateway mandava CTRL+C para ele em vez de só consultar.
- Checagens do apt: atualização automática configurada em outro arquivo do `apt.conf.d`, listas de pacotes atualizadas à mão e uma unidade do systemd deixada desligada de propósito não são mais apontadas para sempre.

### Atualizar

`hermes plugins update hermes-atlas-evolution` em cada perfil que o tem, depois `hermes gateway restart` e `sudo systemctl restart hermes-dashboard` (ou reinicie o Hermes Desktop). A primeira checagem diária do doctor acontece na próxima rodada de aprendizado; `hermes evolution environment scan` a roda na hora.

## 0.8.5

O Atlas Evolution passa a cuidar de todos os perfis de um Hermes de uma vez, num Modo Auto ligado por padrão, e conhece o ecossistema de plugins do Hermes.

### Novidades

- **O modo** (`mode`: `auto` por padrão, `assist`, `manual`; tela 5 do menu, as configurações do painel). Substitui o `auto_actions`, que continua sendo lido quando `mode` não está definido (`off` → manual, `safe` → assist, `full` → auto), então quem o escolheu mantém a escolha; o `settings.json` do próprio plugin, que o agente consegue gravar, só pode restringir para `manual`. No auto, o plugin aplica sozinho só ações reversíveis, em todos os perfis: cópias idênticas da memória, travar skills que o curador reescreve sem parar, se instalar (no commit do próprio líder) nos perfis em uso que nunca o tiveram, mover pastas de instalações que falharam para a lixeira dele (guardadas 30 dias) e `hermes pm gc` no máximo uma vez por semana; no máximo 3 mudanças no Hermes por passada (além de até 10 cópias idênticas da memória e do pm gc semanal), cada uma só começa com tempo para terminar; uma ação que o dono desfaz nunca se repete, seja qual for o perfil líder depois.
- **O ambiente** (`environment.py`; tela 14 do menu, a aba **Ambiente** do painel, `hermes evolution environment [show | scan | catalog]`). Um perfil cuida do Hermes inteiro (dos perfis com o plugin ligado, o padrão quando rodou uma passada nos últimos dois dias, senão o primeiro que rodou), no máximo a cada 6 horas, lendo o config.yaml, os plugins (manifesto, registro de instalação, checkout git e código Python, analisado com `ast` e nunca executado), as skills e o uso de cada perfil; cada arquivo só se for um arquivo comum e até um limite de tamanho. Achados: `plugin_transform_overlap` (dois plugins podem mudar o resultado da mesma ferramenta por um hook de transformação: o código é lido para saber em quais ferramentas cada um age, só algumas, todas menos algumas ou qualquer uma; o Hermes carrega em ondas de `requires_plugins`, em ordem alfabética do nome no manifesto, e fica com o primeiro resultado de cada chamada; plugins em ferramentas diferentes não são apontados; um plugin de segurança nunca é o sugerido para desligar), `plugin_transform_protected` e `plugin_hints_overlap` (dois plugins de proteção, ou o `tool_hints` deste próprio plugin: recomendação, nunca um plugin desligado), `plugin_duplicate` (dois plugins reescrevendo o comando do terminal, ou um papel que só um deveria cumprir), `plugin_removed` (na lista de removidos do Hermes para aquele perfil, como o Hermes lê; o Hermes já se recusa a carregá-lo, então o plugin só mostra o comando de remoção), `plugin_obsolete` (arquivado, retirado, inexistente ou sem manutenção, conferido em 16/11/2026), `plugin_unused` (instalado e nunca carregado), `plugin_leftover` (pastas `.install-*`), `plugin_update` (versão mais nova no catálogo do Hermes, agrupada por plugin), `self_missing` (nunca teve o plugin) e `self_off` (o dono o desligou: mantido), `profile_idle`, `skills_unused` (30 dias, o padrão do próprio curador do Hermes), `skills_duplicate`, `skills_similar`, `skills_shared` (cópias em 3 ou mais perfis: `skills.external_dirs`; as skills que vêm com o Hermes ficam de fora). **Antes de qualquer mudança num perfil, o plugin lê esse perfil de novo do disco** e só roda se a mesma situação ainda estiver lá: uma proposta guardada, a varredura em cache ou arquivos de outro perfil nunca são tomados como verdade. Desligar um de dois plugins é uma tecla naquela proposta, nunca no Aplicar tudo.
- **A base de conhecimento** (`ecosystem.py`, `knowledge/*.json`), copiada para as tabelas `catalog_plugin`, `catalog_removed`, `knowledge_plugin` e `playbook` do `evolution.sqlite3` do perfil líder: o catálogo de plugins do próprio Hermes, lido da cópia que o Hermes guarda em cache (senão do código dele), o que o plugin sabe de plugins conhecidos, os plugins obsoletos do ecossistema e o playbook de cada situação (nível, risco, ação, desfazer, título e recomendação em inglês e português).
- Cada proposta tem uma linha **O que fazer** no menu, nas abas Propostas e Ambiente do painel e no `hermes evolution environment`; mudanças em outros perfis aparecem e rodam como `hermes -p <perfil> ...`.

### Mudanças

- O radar não aponta mais plugins que compartilham um hook de transformação: era confuso e, para plugins em ferramentas diferentes, errado. O ambiente aponta só uma sobreposição real, dizendo quem vale.
- Comandos do Hermes só rodam para um perfil de verdade (com `config.yaml`), então uma pasta de rascunho nunca faz o Hermes preparar uma casa nova; a suíte de testes nunca alcança o Hermes da própria máquina (`ATLAS_EVOLUTION_NO_HERMES=1`).
- O README diz o que o plugin faz na prática e deixa de fora a saída de versões antigas.

### Limites conhecidos

- Quando o perfil que cuida do Hermes inteiro muda, o novo pode mostrar de novo uma sugestão que você rejeitou no antigo (uma mudança automática que você desfez continua desfeita).
- Plugins guardados em pastas de categoria (`plugins/<categoria>/<nome>/`) não são lidos pelo ambiente.

### Atualizar a partir da 0.8.4

Com as configurações padrão, atualizar passa você para o Modo Auto. A primeira passada olha todos os perfis e pode, sozinha: tirar cópias idênticas da memória, travar skills que o curador reescreve sem parar, rodar `hermes pm gc` se versões antigas de ferramentas ocuparem 300 MB ou mais, instalar o plugin em perfis usados nos últimos 7 dias que nunca o tiveram (o Hermes baixa do GitHub) e mover para a lixeira pastas de instalações que falharam. Para olhar antes e decidir à mão, passe o modo para assist antes de atualizar: `hermes config set plugins.entries.hermes-atlas-evolution.settings.mode assist`.

```bash
hermes -p <perfil> plugins update hermes-atlas-evolution   # cada perfil que o tem; sem -p para o padrão
hermes gateway restart
sudo systemctl restart hermes-dashboard
hermes evolution                                           # tela 14
```

## 0.8.4

O Atlas Evolution agora cuida do próprio Hermes e do servidor em que ele roda, sem agente e sem tokens, e mostra para que serve no topo do README, com telas do painel no dashboard e do terminal.

### Novidades

- **Saúde do Hermes** (`hermes_care.py`; tela 12 do menu, a aba nova **Saúde** do painel, `hermes evolution care`). Lida dos próprios arquivos do Hermes, sem rede, sem modelo e sem nenhum comando `hermes`: o gateway vivo (o sinal de vida dele) e a memória dele diante do normal, o banco de sessões (modo de journal, um log de escrita que cresceu, um `quick_check` semanal), o espaço no disco onde o Hermes grava com o dia em que ele encheria no ritmo da última semana, o crescimento de cada pasta do perfil, memória, swap e CPU, versões de ferramentas gerenciadas pelo Hermes que nada referencia, e um gateway que não voltaria depois de um reinício (unidade de usuário sem linger). A cada 6 horas na passada de aprendizado, com 21 dias de histórico. O `hermes pm doctor` (sem rede), o `hermes doctor`, o `hermes doctor --fix` depois de `hermes backup --quick` e o `hermes pm gc` do próprio Hermes rodam com um clique do dono.
- **A atualização do sistema operacional** (`system_update.py`; tela 13 do menu, `hermes evolution system check|plan|upgrade|repair|report`), Linux com apt. Uma checagem diária só de leitura; um plano (`apt-get update` e uma simulação que aponta pacotes que reiniciam os containers, pedem reinício, mexem no acesso remoto ou no gerenciador de pacotes); e uma atualização só do dono, como root no próprio terminal: a checagem precisa passar, o perfil do Hermes e o código do plugin precisam ser do root, uma cópia da versão de cada pacote e do `/etc` é guardada numa pasta só do root fora de todo perfil (`/var/lib/hermes-atlas-evolution/system/`, as 5 últimas execuções), o `apt-get --with-new-pkgs upgrade` roda numa unidade systemd própria mantendo os arquivos de configuração do dono e sem reiniciar serviço nenhum sozinho, e depois dpkg, apt, cada serviço que rodava e o Hermes são checados de novo, e um relatório lista o que mudou e como voltar. Ocupado quer dizer uma trava do dpkg ou do apt realmente em uso (`/proc/locks`) ou um gerenciador de pacotes rodando. Os pacotes deixados de fora são anotados antes de serem retidos e liberados quando a execução termina, falha ou é parada (um pacote só sai da lista quando realmente liberado, esperando até 3 minutos por um dpkg que ainda termina; `nome:arquitetura` comparado como o apt mostra); uma execução que sumiu é relatada, nunca esperada, e um reparo (também numa unidade própria) libera o que ela ainda retinha e termina uma atualização que uma queda de energia interrompeu. Nunca reinicia a máquina.
- **O resumo semanal com uma tecla**: a sugestão do menu ou `hermes evolution schedule-digest --deliver <local|canal>` cria um job de cron do Hermes que roda um script, não o agente; uma vez só; desfeito pelo histórico (`hermes cron remove`).
- O `status` e o menu mostram o que pede atenção na saúde do Hermes e na do sistema, com os valores; sugestões novas (terminar uma atualização interrompida, instalar atualizações de segurança, liberar versões antigas de ferramentas, agendar o resumo); as telas 12 e 13 vêm depois das que já existiam, então os números que você conhece não mudam.
- O README começa com para que o plugin serve e telas do painel e do terminal (tiradas de um Hermes 0.23.1 real com dados de demonstração); a atualização está em `docs/pt-BR/upgrading.md`; a saúde do Hermes e a atualização do sistema em `docs/pt-BR/maintenance.md`.

### Mudanças

- Entradas da memória que diferem num número (uma porta, uma versão, um id) são dois fatos, não uma repetição; a lista de repetições mostra no máximo 100.

### Testado

Além dos testes unitários: a suíte em containers com Ubuntu 26.04 (Python 3.14), Ubuntu 24.04 (3.12) e Debian 13 (3.13); atualizações completas de imagens do Ubuntu 24.04 e 22.04 de abril de 2024 (67 e 55 pacotes) com um pacote deixado de fora e um arquivo de configuração editado mantido; uma queda de energia no meio do dpkg, detectada e reparada, o pacote que ela retinha liberado; a atualização parada como o `systemctl stop` faz; uma trava do dpkg segurada por outro programa; um Hermes de outro usuário, recusado; uma atualização do Ubuntu 26.04 com o Hermes 0.23.1 instalado, pelo comando do plugin; um Hermes 0.23.1 real num container (dois perfis, o dashboard com login por senha, `hermes curator lock`, `hermes cron create`, `hermes pm doctor`); e testes de estresse (8 processos aplicando uma proposta, 200 passadas de aprendizado, 100 mil arquivos, 5 mil propostas). Um gerenciador de pacotes trabalhando há mais de 6 horas pede atenção; serviços que terminaram sem erro aparecem à parte dos que falharam; um gateway só conta como caído sem sinal de vida recente (um em outro namespace de PID não). Duas rodadas de revisão independente da atualização do sistema e das checagens de saúde levaram à pasta das execuções só do root, à exigência de um Hermes do root, ao teste de ocupado pelas travas, às retenções liberadas, às execuções perdidas e à comparação mais rigorosa dos serviços descritas acima.

### Atualizar a partir da 0.8.3

```bash
hermes -p <perfil> plugins update hermes-atlas-evolution   # cada perfil; sem -p para o padrão
hermes gateway restart
sudo systemctl restart hermes-dashboard
hermes evolution                                           # telas 12 e 13
```

## 0.8.3

O Atlas Evolution agora age sozinho onde é seguro e diz em palavras simples o que cada proposta significa. Num perfil real, 16 propostas esperavam com códigos como `skill_lock` e `[medium]`, e nada acontecia até o dono rodar cada comando à mão; uma skill tinha sido reescrita 75 vezes numa semana.

### Novidades

- **Ações automáticas** (`auto_actions`, `safe` por padrão). No fim de cada passada de aprendizado, `safe` tira cópias de uma entrada da memória que são exatamente o mesmo texto, caractere por caractere (guarda uma cópia do arquivo; uma entrada que o dono devolve fica); `full` também trava as skills que o curador do Hermes reescreve sem parar, com `hermes curator lock`, até 3 por passada. Toda ação fica no histórico com `automatic` como autor e o desfazer (`hermes curator unlock`, a cópia da memória); uma mudança automática que o dono desfaz nunca mais é aplicada sozinha; uma falha aparece no resultado e nunca interrompe a passada. A configuração só vale vindo da configuração do Hermes, como `runtime_guard: false`. A configuração recomendada agora inclui `full`.
- **Aplicar agora.** Uma mudança reversível que uma proposta pede (hoje, travar uma skill) é aplicada pelo plugin com o próprio comando do Hermes, pelo menu, pelo painel ou pelo `proposals approve`, uma a uma ou todas de uma vez, e desfeita pelo histórico.
- **Propostas em palavras simples** (`explain.py`): título, risco, o que foi visto com os números, o que aplicá-la faz e como desfazer, em português do Brasil ou inglês, no menu e no painel, agrupadas em mudanças que o plugin aplica, lições, mudanças que você aplica e observações. Uma observação é dispensada com uma tecla ou um clique; uma falha que nenhum modelo fixo cobre ganha a lição escrita pelo menu.
- **O resumo do menu** mostra o que pede atenção em frases completas e até cinco sugestões com letras que agem na hora. Uma tela nova, **Ações automáticas** (5), mostra o modo e as ações automáticas dos últimos 7 dias com o desfazer, e muda o modo; as telas seguintes desceram um número.
- O `status` e o cabeçalho do painel mostram o modo e as ações automáticas dos últimos 7 dias; o `learn --notify-only` também.
- Este arquivo, e uma seção de novidades e um começo rápido no topo dos dois READMEs.

### Atualizar a partir da 0.8.2

```bash
hermes -p <perfil> plugins update hermes-atlas-evolution   # cada perfil; sem -p para o padrão
hermes gateway restart
sudo systemctl restart hermes-dashboard
```

As ações automáticas começam em `safe`. Para deixar o plugin travar skills instáveis sozinho: a sugestão do menu, a tela 5, a configuração recomendada ou `hermes -p <perfil> config set plugins.entries.hermes-atlas-evolution.settings.auto_actions full`.

## 0.8.2

O painel da 0.8.1 não conseguia decidir num dashboard real de produção: atrás de HTTPS o Hermes chama o cookie de login de `__Host-hermes_session_at`, e todo perfil que não é o do próprio dashboard roda o plugin no seu plugin host, que o Hermes alcança sem cookie nenhum. Agora o painel só decide no processo do próprio dashboard, com a sessão que a porta de login do Hermes verificou, e o painel de qualquer outro perfil pergunta a esse processo, nomeando o perfil na tela. Duas revisões independentes: a primeira derrubou a primeira correção (o veredito encaminhado ao plugin host podia ser enganado), a segunda não achou caminho para decidir sem login.

### Novidades

- **Menu interativo no terminal:** `hermes evolution` sem comando, só no terminal do dono (nunca no shell do agente): lições, aprovar todas, propostas, histórico com desfazer, configurações e a configuração recomendada, quem é você nos chats, memória do Hermes, resumo, situação e passada de aprendizado.
- **Memória do Hermes enxuta:** mostra o que se repete no `MEMORY.md` e no `USER.md` e tira a cópia que você escolhe, sob a trava de memória do próprio Hermes, com uma cópia do arquivo guardada e desfazer.
- `admin.py`: as regras compartilhadas pelo painel e pelo menu.

### Correções

- O painel reconhece um login de verdade (inclusive o cookie `__Host-` do HTTPS) e decide por qualquer perfil escolhido no dashboard; um ticket do Hermes Desktop só pelo próprio perfil.
- `lessons: off` gravado pelo `hermes config set` (que vira `false`) passou a ser lido como `off`; as configurações são gravadas e registradas uma a uma; um `hermes config set` travado vira recusa, não erro; arquivos de memória com BOM são lidos como o Hermes lê.

## Versões anteriores

A 0.8.1 (decisões pelo painel, configurações sem reiniciar), a 0.8.0 (painel no dashboard, pacotes de lições, guarda de runtime refeita) e as anteriores estão descritas no [CHANGELOG em inglês](CHANGELOG.md).
