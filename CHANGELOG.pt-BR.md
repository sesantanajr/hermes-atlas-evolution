# Mudanças

*In English: [CHANGELOG.md](CHANGELOG.md), with every version.*

## 0.11.0

Um índice que se explica, uma instalação que a própria varredura do Hermes considera segura e a pilha obsoleta removida.

### Novidades

- **Por que o Índice de evolução mudou.** Quando uma parte mudou 5 pontos ou mais em relação a uma semana atrás, o `status` e o resumo dizem qual parte e o que a moveu: as ferramentas cujos erros mais cresceram ou caíram, turnos com falha e lentos, as suas correções, as lições que não ajudaram, os problemas de saúde encontrados agora (`O que o moveu: ferramentas 92 → 62 · terminal +147 erros`).
- **Uma falha que disparou hoje.** Uma falha que já aconteceu pelo menos 5 vezes no dia UTC e 4 vezes a sua média diária dos 14 dias anteriores aparece no que precisa de atenção, com a ferramenta e as duas contagens: uma dependência, uma chave, um serviço ou uma cota que acabou de mudar, dito no mesmo dia. Uma rodada agendada silenciosa (`learn --notify-only`) avisa cada uma uma vez por dia.
- **Faça primeiro.** Até 3 passos em ordem dos pontos do índice que cada um pode somar, estimados com a própria fórmula do índice: uma lição que espera por você, uma falha recorrente sem lição, um problema de saúde. Cada um com o seu comando. No `status`, no resumo e na Visão geral do painel ("O que os números dizem"). Sem chamar modelo: poucas consultas indexadas sobre 7 a 14 dias.
- **Autorreparo do `lessons: false`.** O `hermes config set ... settings.lessons off` grava o booleano falso, que o Hermes apontava em todo carregamento e o formulário de configurações mostrava como nenhuma opção. Agora o plugin grava de volta o texto `off` uma vez, com o gravador do próprio Hermes para as suas configurações (`ctx.set_config`).
- **Uma tarefa antiga do tick é encontrada.** Uma tarefa do agendador do Hermes que ainda roda `evolution ops tick` ou `evolution tick` falha em toda execução na 0.11.0: o `status` a mostra com o comando `cron remove` exato.
- **As notas da 0.1 podem ser encerradas.** Ao lado do comando só de leitura que as mostra, o `status` dá um que as salva em `evolution/notes-0.1.json` e as tira do banco; o item então some.

### Correções

- **A varredura de instalação do Hermes diz `safe`.** A 0.10.x recebia `caution` da varredura de plugins do Hermes (`plugin-guard-v15`), por causa de textos de orientação que mostravam `sudo loginctl`, `sudo reboot`, `sudo dpkg-reconfigure` e `sudo journalctl`, e de dois exemplos do CHANGELOG de um pipe para o `sh`. Uma instalação `caution` espera um sim de um terminal, então a instalação do próprio plugin em outro perfil (Modo auto, `update --all`) era recusada ali. A orientação agora diz `loginctl enable-linger $USER` (ele pede a senha quando precisa), `sudo systemctl reboot`, `dpkg-reconfigure -plow unattended-upgrades` como root e `sudo systemctl status <serviço> -n 30 --no-pager`. O plugin nunca roda nenhum deles por conta própria; um teste novo mantém esses textos fora.
- **Host de plugins** (`plugins.isolation: host`): o comando de terminal `evolution` não é mais registrado ali (o Hermes o pula com um aviso em todo carregamento), e o `status` diz que a Proteção do Hermes está desligada ali mesmo quando é lido dentro do host.
- **Um escopo hospedado que recusa a troca de perfil** (`ScopeSwitchRefused`) não derruba mais a rodada de aprendizado: a rodada roda no escopo em que está.
- **A aba do painel** vem depois de Skills: Analytics fica oculta por padrão, então `after:analytics` a colocava no fim.

### Removido

O que a 0.10.0 marcou como obsoleto, cerca de 5.300 linhas e 14 módulos (`supervisor`, `linux_actions`, `learning`, `reasoning`, `inventory`, `linux_host`, `workloads`, `ops_metrics`, `semantic_ops`, `advisory`, `native_actions`, `diagnostics`, `memory`, `native_signals`):

- os comandos `ops ...` (o supervisor de operações Linux da 0.3), `health` (use `care check`), `tick` (a rodada roda sozinha; `learn` roda uma agora), `feedback`, `history` (use `decisions`), `memory remember|recall|audit|forget`, o comando de chat `/evolution` e a lembrança automática das notas da 0.1 nos turnos;
- os hooks `subagent_stop`, `post_curator_run` e `on_kanban_worker_exited` (ficam 14 hooks);
- as configurações `sensitivity`, `inspect_shared_kanban`, `kanban_handoff`, `linux_supervisor`, `semantic_incident_model`, `daily_estimated_budget_usd`, `legacy_memory_commands` e `automatic_shared_recall`, agora ignoradas sem erro;
- o `supervisor.sqlite3`, que não é mais gravado; o executor avulso ignora `--hermes-version`;
- as páginas de Operações da documentação: `usage` e `rates_usd_per_million` foram para [Uso e custo](docs/pt-BR/evolution-loop.md#uso-e-custo).

### Documentação

- [Atualizar para a 0.11.0](docs/pt-BR/upgrading.md#atualizar-para-a-0110): o que substitui cada comando removido, a tarefa agendada antiga, as notas da 0.1.
- A rota do refletor, como o Hermes a resolve: o provedor e o modelo da tarefa de compressão só chegam a ele com `plugins.entries.hermes-atlas-evolution.llm.allow_task_override: true`; caso contrário, configure `auxiliary.evolution_reflector`.
- As tabelas de hooks listam os 14 hooks do `plugin.yaml`; cada link e âncora foi conferido nos dois idiomas.

## 0.10.1

Um plugin mais enxuto e a documentação refeita em dois idiomas.

### Mudanças

- **Instalação mais leve.** O Hermes instala um plugin com `git clone --depth 1` do ramo padrão em cada perfil, e as imagens do README (cerca de 3 MB) iam junto, sem uso. Agora elas ficam no ramo `assets` do repositório e o README e a documentação as carregam de lá: a cópia de cada perfil caiu de cerca de 5,2 MB para cerca de 2,2 MB.
- **Documentação em pt-BR e en-US.** As páginas em inglês foram para `docs/en-US/`, lado a lado com `docs/pt-BR/`; cada idioma tem um início da documentação, um novo guia de **Primeiros passos** (instalar, a primeira hora, deixar do seu jeito, toda semana, atualizar e remover) e um novo guia de **Solução de problemas** (o gateway depois de uma atualização do Hermes, perfis fixados, um plugin que não carrega, o Índice de evolução, o custo do próprio plugin, um banco danificado, a Proteção do Hermes, configurações que esperam um reinício, a remoção completa). Cada link foi conferido.
- **Um README moderno** nos dois idiomas: o que é e por quê, destaques, um diagrama de como funciona, os modos, a compatibilidade e as telas.
- **CI** com `actions/checkout@v5` e `actions/setup-python@v6`, o ambiente Node 24 atual do GitHub Actions.
- A página de Operações diz que está obsoleta (sai na 0.11.0).

## 0.10.0

A evolução do agente como um número, um plugin que mede quanto custa, e o mesmo trabalho feito com menos: uma passada
de medição nos ganchos, na rodada de aprendizado e na leitura de todos os perfis, e uma auditoria de cada módulo.

### Novidades

- **O Índice de evolução** (`evolution_index`): um número de 0 a 100 por perfil e por dia (UTC), a partir do que o plugin já mede nos últimos 7 dias: ferramentas (chamadas que não falharam), turnos (nem falharam nem foram lentos), você (quão raramente você corrigiu o agente ou perguntou de novo), lições (das lições ativas já julgadas, a parte que cortou a falha pela metade) e saúde (as checagens do Hermes e do servidor: o que para o agente tira 25 pontos, o que é para arrumar logo 8, manutenção de rotina nada). É a média ponderada das partes com dados (pelo menos duas), guardada a cada rodada de aprendizado (90 dias), e aparece com as partes e a tendência contra uma semana atrás no `status`, no resumo e no primeiro quadro da Visão geral do painel.
- **Quanto o plugin custa ao agente** (`selfcost`): cada gancho é cronometrado onde roda (cerca de um microssegundo de medição), cada processo grava os números a cada 10 minutos, e o `status` mostra a mediana e o p95 por chamada de ferramenta, por turno e por prompt. Acima de 50 ms de p95 em 200 chamadas, o `status` avisa.

### Mais rápido

- A seção de lições do prompt é montada no máximo a cada 30 segundos por tipo de sessão, e de novo na hora depois de uma rodada de aprendizado ou de uma mudança de configuração no mesmo processo: ela era lida do banco a cada prompt (de 3,8 ms para 0,02 ms por prompt).
- Os contadores e o resultado de uma chamada de ferramenta são uma transação de gravação em vez de duas, cada atualização ainda no seu próprio savepoint; o gravador compartilhado confere a identidade do arquivo do banco a cada 2 segundos em vez de a cada gravação (de 0,5 ms para 0,3 ms por chamada).
- Skills quase iguais são comparadas como máscaras de bits, ordenadas por tamanho para que um conjunto só encontre os que podem casar (8 vezes mais rápido com 400 skills, os mesmos pares); um `SKILL.md` só é lido e calculado de novo quando o tamanho ou a hora de mudança mudam.
- Os módulos que só os comandos obsoletos usam (`diagnostics`, `memory`, `advisory`, `native_actions`) não são mais importados por todo processo do Hermes ao carregar.

### Correções

- Um nome de perfil, uma raiz e uma lista de perfis em todo lugar (`environment.root_of`, `environment.profiles`, `admin.profile_name`): um perfil que é um link recebia o nome do destino no `care` e num pacote exportado, e o `status` listava pastas (links, nomes que o Hermes recusa) de que o Modo Auto nunca cuida.
- O `care --help` cita `fix-gateway` e `undo-gateway`; os subcomandos de `ops` e `memory` têm uma linha de ajuda; o `ops review --days` está documentado de 1 a 35, como o comando aceita; a docstring do pacote traz o nome dele.

### Obsoleto (sai na 0.11.0)

- `health` (use `care check`), `history` (use `decisions`), `tick`, `feedback`, `memory` e o comando de chat `/evolution` (as notas da 0.1; `memory audit` lista todas) e `ops` (o supervisor de Linux da 0.3 e seus ensaios). Nenhum deles é usado por padrão, pelo menu, pelo painel ou pela rodada de aprendizado; agora cada um avisa quando roda, no stderr.

### Removido

- A ferramenta do agente `hermes_atlas_evolution_plan`: devolvia um texto fixo e não lia dado nenhum. O `hermes evolution ops plan` imprime o mesmo plano até a 0.11.0; o `agent_tools` agora dá ao agente só a ferramenta de lições.
- Código que nada usava (`environment._similar`, `hermes_care._in_use`, `admin.LISTS`, `explain._FAILURE`, `logwatch.RESULTS`, `proposals.RISKS`).

## 0.9.2

Autoreparo e todos os perfis atualizados sozinhos. Uma segunda revisão completa (seis passadas independentes pelo plugin inteiro) corrigiu o que a 0.9.1 ainda errava, a começar pelos perfis fixados, que a 0.9.1 dava como atualizados enquanto o Hermes os mantinha no commit antigo.

### Novidades

- **Todos os perfis no mesmo código, sozinho (Modo Auto).** O perfil que cuida do Hermes inteiro leva a cópia do plugin de cada outro perfil ao commit dele: uma cópia fixada em outro commit, mais antiga, com outro código na mesma versão, ou deixada pela metade por uma atualização interrompida. No máximo dois perfis por rodada, uma vez por situação, nova tentativa no dia seguinte se o Hermes recusar; nunca onde o plugin está desligado, onde o modo daquele perfil é Assistido ou Manual, nem num link de desenvolvimento. Nada novo é baixado sozinho: uma cópia sem fixação só é atualizada enquanto o último commit do repositório for o do líder. Cada sincronização fica no histórico.
- **Autoreparo do banco do plugin.** Um `evolution.sqlite3` danificado (gravação interrompida, disco cheio, uma ferramenta de sincronização copiando o arquivo no meio da gravação) parava toda rodada, lição e contador enquanto o processo vivesse. Agora o dano é confirmado pela própria checagem do SQLite, o arquivo é posto de lado como `evolution.sqlite3.corrupt-<hora>` (os dois mais novos ficam) e um novo começa; o log avisa.
- **Uma versão por gateway em execução.** O gateway e o dashboard importam todos os módulos do plugin ao subir, então uma atualização gravada em disco com ele rodando nunca mistura módulos novos e antigos nesse processo.
- **O gateway se repara sozinho (Modo Auto).** Quando o serviço de usuário do próprio gateway aponta para um Python que o Hermes removeu (systemd `status=203/EXEC`, reinício em loop) e nenhum gateway está rodando, o perfil que cuida do Hermes o aponta para o Python que o Hermes usa agora, como o `care fix-gateway` faz: uma cópia do arquivo é guardada e o histórico mostra o reparo com o desfazer (`hermes evolution care undo-gateway`). No máximo uma vez por dia; nunca com um gateway rodando, numa unidade desligada que não está falhando, nem no escopo do sistema.
- **Retenção sem o autopilot.** Com `autopilot_minutes: 0`, o banco continua descartando contadores antigos e os trechos do refletor uma vez por dia.

### Correções

- **Perfis fixados nunca eram atualizados.** O Hermes guarda os registros de instalação em `plugins/.install-metadata.json` (um por plugin), mantém a fixação quando um plugin é instalado de novo sem `--ref` e recusa o `plugins update` numa cópia fixada. O `hermes evolution update`, o `install.sh` e as orientações liam a fixação do arquivo errado e reinstalavam sem `--ref`, então o Hermes reinstalava o commit fixado e o perfil aparecia como atualizado. Agora a fixação passa para o último commit com o próprio `plugins install <url> --force --ref <commit>` do Hermes; uma cópia sem manifesto é instalada de novo; o `--enable` só entra onde o plugin já está ligado, então uma atualização nunca liga o plugin onde o dono o desligou; um link de desenvolvimento nunca é mexido.
- **O `hermes pm gc` podia apagar o Python de onde o serviço do gateway ainda inicia** (depois que o `facts.json` do Hermes mudava, a pasta só sumia do relatório). Agora a checagem de saúde encontra essa pasta e avisa, e o `pm gc` fica bloqueado (Modo Auto, `care gc`, o menu) até o serviço apontar para o Python atual, o que o `care fix-gateway` faz.
- **Reparo do gateway e status:** o `care fix-gateway` olha a unidade que o Hermes dá a este perfil (`hermes-gateway-<perfil>`, ou a da raiz quando ela atende o perfil) em vez de um nome fixo, recusa onde o systemd não pode ser consultado (com `sudo`), deixa em paz uma unidade que sobrou enquanto outro gateway roda, só recarrega quando o arquivo já estava certo e guarda o registro para desfazer; depois disso o status deixa de dizer que o gateway não sobe (e volta a dizer depois de um desfazer). Os arquivos das unidades são gravados com fsync.
- **Checagens do Linux:** um `dpkg --audit` ou `apt-get check` cortado pelo tempo é "não verificado", nunca "quebrado"; o erro de barramento do systemd 254 e códigos de saída inesperados deixam o escopo não verificado em vez de vazio; um programa que este usuário não consegue ver nunca é dado como ausente; a unidade do gateway deste perfil é escolhida, nunca a de outro perfil; um perfil com nome atendido pelo gateway da raiz lê o estado da raiz; uma atualização do sistema tem limite de tempo (`RuntimeMaxSec`).
- **Uma proposta que você recusou era aplicada sozinha 30 dias depois** no Modo Auto (ela reabre para você ver; a rodada automática não sabia que você tinha dito não). Agora uma recusa impede que ela seja aplicada sozinha, em qualquer perfil; desfazer a recusa libera.
- **Comandos só do operador:** um agente passava pela checagem depois de limpar os marcadores do ambiente quando os argumentos de um processo do Hermes acima dele tinham a palavra "evolution" (um perfil chamado evolution, uma pergunta avulsa que a cita). Agora só o lançador do próprio comando é pulado, reconhecido pela forma dos argumentos.
- **Proteção do Hermes:** comandos que reiniciam ou matam o gateway, apagam o Hermes ou mudam este plugin ainda passavam quando escritos como: continuação de linha (`\` no fim da linha); um `#` dentro de uma palavra (uma URL, `$#`) lido como comentário; um `<<EOF` dentro de aspas ou de um comentário; um shell que lê os comandos de um heredoc, de `<<<` ou de um pipe (`bash <<'EOF'`, a saída de um `echo` ligada ao `sh` por pipe); `kill -- -$PPID` ou `pkill -u $USER`; `cmd /c` e `powershell -Command` com caminhos do Windows, `taskkill /IM python.exe`; `find ... ! -name X -delete` ou `-o`; `grep -F hermes | xargs kill`; uma gravação nos arquivos do próprio plugin (`sed -i`, `: >`, `tee`). Agora todos são barrados. Ela não barra mais `find ~/.hermes -name '*.log' | xargs rm`, `cd ~/.hermes && ls logs/*.log | xargs rm` nem a leitura do `config.yaml`, e um comando com muitos grupos `{a,b}` não leva mais minutos para ser checado (uma checagem travada bloqueia a ferramenta).
- **Texto escondido dos terminais do agente:** o `report` e o `learn` mostravam textos de lições, rascunhos e lições de propostas que o `lessons list` esconde do terminal de um agente; agora escondem também.
- **Memória:** a remoção automática de repetidas podia tirar a única cópia que o agente vê (a mesma entrada no `MEMORY.md` e num `USER.md` que o Hermes não carrega); uma remoção cujo registro não podia ser gravado derrubava a rodada inteira.
- **Aprendizado:** uma falha que o refletor recusou era perguntada de novo a cada rodada, então as mesmas poucas falhas gastavam a cota do dia; uma sessão danificada no histórico do Hermes parava o replay para sempre (e com ele as bases das lições); uma rodada manual não tinha prazo e podia passar da reserva; o registro do Curator era lido segurando a trava de escrita.
- **Painel:** outro perfil era comparado com a versão do plugin do próprio dashboard e podia mostrar a lista de plugins dele; os ids de remetente do dono chegavam a sessões só de leitura; o painel e as recusas podiam falar idiomas diferentes; um id grande demais para o SQLite dava um 500 cru; um erro de permissão do sistema era chamado de "gerenciado pelo administrador"; o custo do resumo agora avisa quando inclui sessões que começaram antes do período.
- **Linha de comando:** todo comando impresso nomeia o perfil (`hermes -p default ...`), porque o Hermes pode seguir um perfil fixo; um comando que falhou (`update`, `care`) sai com código 1, então `hermes evolution update --all --yes && hermes gateway restart` não reinicia mais depois de uma atualização que falhou; Ctrl-C mostra uma frase, não um traceback; a dica sem terminal repete o seu próprio pedido; `owner add/remove` recusa onde os donos atuais não podem ser lidos, em vez de imprimir uma lista que os apagaria; decisões de cron e de manutenção aparecem descritas corretamente; um `HERMES_HOME` vazio não é um perfil.
- **Windows:** o `NUL` não é mais tomado por um terminal, e o idioma segue a localidade do Windows.
- **Perfis:** uma pasta de perfil que é um link para outro lugar não é mais tomada pelo perfil padrão (o que a tornava um segundo líder).
- **`install.sh`:** cada perfil roda com o próprio `HERMES_HOME` e `--profile` (um perfil fixo ou exportado não faz mais atualizar o perfil errado); um nome de perfil que o Hermes recusaria, um nome repetido, uma pasta `profiles` mais acima no caminho, um `HOME` ausente e a falta de terminal (um script do `curl` ligado ao `sh` por pipe, sem `-t`) são tratados; o reinício do gateway nunca lê o resto de um script recebido por pipe, e uma falha nele aparece no código de saída.

### Removido

- Chaves que só a suíte de testes usava: `ATLAS_EVOLUTION_NO_HERMES` e `ATLAS_EVOLUTION_HOST_CHECKS`, e as referências a arquivos de teste no código e na documentação. O workflow de CI é o `checks.yml`.

## 0.9.1

Versão de estabilidade: uma revisão completa do plugin corrigiu o que podia travar, vazar memória, perder dados ou dar orientação errada. O plugin agora se atualiza em todos os perfis e repara o problema do gateway que antes passava despercebido.

### Novidades

- **`hermes evolution update`**: atualiza o plugin em todos os perfis (`--all`), nos que você nomear (`--profiles default,trabalho`) ou nos que escolher numa lista. Um perfil fixado num commit é reinstalado sem a fixação; uma atualização recusada cai para uma reinstalação forçada; um perfil com erro nunca impede os outros. `--list` mostra versão, commit e fixação de cada perfil; `--install-missing` também instala onde falta.
- **`install.sh`**: o mesmo, sem depender do plugin carregado (primeira instalação ou cópia quebrada). `sh install.sh` pergunta os perfis; `sh install.sh --all --yes --restart` faz tudo e confere o serviço do gateway antes de reiniciá-lo.
- **O gateway que não sobe.** Quando o Hermes troca o Python embutido e o serviço do gateway continua chamando o antigo (systemd `status=203/EXEC`, reinício em loop, `hermes gateway restart` esperando minutos), o `status`, o `care` e o painel agora avisam, com o arquivo que tem o caminho antigo e os comandos que resolvem. `hermes evolution care fix-gateway` faz o reparo (guarda cópia do arquivo; `care undo-gateway` desfaz). Reinício em loop, gateway preso em starting/restarting e dados de saúde desatualizados também são apontados. A sugestão de `hermes pm gc` nunca oferece um runtime que um serviço ainda usa.

### Correções

- O Modo Auto instalava o plugin em outros perfis com `--ref <commit>`, o que os fixava, e o `hermes plugins update` falhava ali com "is pinned to". Agora instala sem ref; cópias fixadas ou defasadas aparecem com o comando que remove a fixação.
- Ganchos: um registro ruim (um caractere quebrado num erro de ferramenta) não para mais toda a gravação até reiniciar; a limpeza diária não roda mais dentro de uma chamada de ferramenta; o acompanhamento do turno não para quando a gravação de feedback falha; conexões fechadas não são reabertas; mensagens longas e caches por turno têm limite; recarregar o plugin não mantém o antigo na memória.
- Proteção do Hermes: não barra mais encerrar seus próprios processos só porque o caminho tem "hermes" ou por `-u root`; `find ... -name X -delete` dentro da pasta do Hermes é permitido; here-strings, aninhamento mais profundo, `$(...)` entre aspas e padrões patológicos são lidos corretamente; `reboot`, `shutdown`, `systemctl reboot` e `loginctl terminate-user` contam como derrubar o gateway.
- Checagens do Linux: unidades de usuário são lidas com o barramento certo como root, comandos que estouram o tempo não deixam processos filhos, a checagem de atualização respeita seu tempo, relatórios são gravados de forma atômica, `unattended-upgrades` conta como ocupado, um gateway vivo não é dado como caído quando o Hermes não gravou a hora de início, o `hermes pm gc` não passa do tempo da manutenção.
- Correções menores: travas de skills em pastas profundas, o watchdog acha o `hermes` fora do PATH, requisições malformadas no painel respondem com mensagem em vez de um 500 cru, jobs de resumo duplicados, limites de `decisions list --limit` e `ops review --days`, a tela Ambiente do terminal mostra o mesmo modo do painel.

### Removido

- A suíte de testes não é mais distribuída com o plugin.

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
