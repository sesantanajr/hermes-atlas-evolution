![Atlas carregando uma esfera celeste sobre uma grade técnica preta. Atlas Evolution.](docs/banner.jpg)

# Atlas Evolution

[English](README.md) | **O Atlas Evolution cuida do seu Hermes Agent por você.** Ele olha todos os perfis do seu Hermes (os erros do agente, as skills, a memória, os plugins, o gateway e o servidor), conserta sozinho o que é seguro consertar, sem usar o agente e sem gastar tokens, e transforma o resto em sugestões curtas, em português, com os números, uma recomendação clara e uma tecla para aplicar. Tudo o que ele muda pode ser desfeito.

[![Licença: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Hermes Agent 0.23.1 ou superior](https://img.shields.io/badge/Hermes%20Agent-%E2%89%A5%200.23.1-6f42c1.svg)](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1)
[![Linux](https://img.shields.io/badge/atualiza%C3%A7%C3%A3o%20do%20sistema-Ubuntu%20%7C%20Debian-e95420.svg)](docs/pt-BR/maintenance.md)

## O que ele faz, na prática

Um Hermes que roda há semanas acumula problemas que ninguém vê: o agente repete o mesmo erro, dois plugins brigam pelo mesmo trabalho, plugins instalados nunca carregam, skills ficam paradas ou duplicadas, a memória enche de repetições (e ela vai em todo prompt), o disco enche, o servidor fica sem atualização. O Atlas Evolution (`hermes-atlas-evolution` no Hermes) encontra isso e resolve:

| | Na prática |
|---|---|
| **Para de repetir erros** | Um erro de ferramenta que volta vira uma lição de uma frase que o agente passa a ler. O plugin mede se a lição ajudou e tira as que não ajudaram. |
| **Arruma o ambiente inteiro** | Olha todos os perfis de uma vez e aponta plugins que mudam o resultado da mesma ferramenta (e qual deles se perde), dois plugins fazendo o mesmo trabalho, plugins removidos do catálogo do Hermes, arquivados ou sem manutenção, plugins instalados que nunca carregam, skills sem uso, duplicadas, parecidas ou copiadas em todos os perfis, e restos de instalações que falharam. Conhece o catálogo oficial de plugins do Hermes (mais de 2.300) e quais plugins estão arquivados ou sem manutenção. |
| **Economiza tokens** | Tira repetições da memória do Hermes, trava skills que o curador reescreve sem parar e mostra o que deixa cada chamada pesada. |
| **Mantém o Hermes saudável** | Gateway vivo ou travado, banco de sessões, disco (e o dia em que ele enche), memória, swap, CPU, versões antigas de ferramentas. Roda o `doctor`, o reparo e a limpeza do próprio Hermes, com backup antes. |
| **Mantém o servidor atualizado** | Atualiza o Ubuntu ou o Debian com checagens antes e depois, cópia de segurança, os seus arquivos de configuração mantidos e um reparo se a energia cair no meio. |
| **Protege o Hermes do próprio agente** | Barra comandos do agente que derrubariam ou apagariam aquilo em que o Hermes roda. |

### O Modo Auto (padrão)

No **Modo Auto** o Atlas Evolution pensa e age por você, em todos os perfis do Hermes, sem precisar estar instalado em cada um para enxergá-los. Sozinho, ele só faz o que é seguro e pode ser desfeito:

- tira cópias idênticas de uma entrada da memória (guarda uma cópia do arquivo);
- trava skills que o curador reescreve sem parar;
- se instala, na mesma versão, nos perfis em uso que nunca o tiveram (nunca onde você o desligou ou removeu);
- move para a lixeira as pastas deixadas por instalações que falharam;
- libera versões antigas de ferramentas, no máximo uma vez por semana.

O que poderia quebrar algo (desligar um de dois plugins, remover plugins, arquivar skills) espera você, com o motivo e a tecla ou o comando para aplicar. Antes de mudar qualquer coisa num perfil, o plugin lê esse perfil de novo do disco: uma sugestão guardada nunca é tomada como verdade. Cada ação vai para o histórico com o desfazer, e uma ação que você desfaz nunca se repete. Prefere decidir tudo? Troque para **Assistido** ou **Manual** no menu (tela 5) ou no dashboard.

Os dados ficam na sua máquina, o plugin não abre conexão de rede própria e o agente nunca decide nada.

## Veja como é

### No dashboard do Hermes

A aba **Evolution** do dashboard do Hermes (e do Hermes Desktop). Com login no dashboard, você decide ali, por qualquer perfil.

![Visão geral: lições ativas, o que espera a sua decisão com Aprovar todas e os comandos para levar lições a outro perfil](docs/screenshots/panel-overview-pt.png)

![Propostas em palavras simples: travar skill reescrita em excesso, com o que foi visto, o que faz, como desfazer e o botão Aplicar agora](docs/screenshots/panel-proposals-pt.png)

![Saúde: o gateway, disco e memória livres, swap, o banco de sessões, versões antigas de ferramentas, atualizações do sistema e reinício pendente, com o que pede atenção](docs/screenshots/panel-health-pt.png)

![Ambiente: cada perfil com as sessões, plugins e skills, o catálogo de plugins do Hermes e o que fazer, cada item com o que foi visto e a recomendação](docs/screenshots/panel-environment-pt.png)

### No terminal

`hermes evolution` abre o mesmo, como um menu numerado, no seu próprio terminal (nunca no shell do agente): o que pede atenção, sugestões com letras que agem na hora e 14 telas.

![O menu do terminal: resumo, o que pede atenção, sugestões de a até f e as telas numeradas](docs/screenshots/terminal-menu-pt.png)

![A tela Ambiente: todos os perfis deste Hermes, o catálogo de plugins e o que arrumar neles](docs/screenshots/terminal-environment-pt.png)

![Um achado aberto: dois plugins mudam o resultado da mesma ferramenta, qual deles vale, o que fazer, o comando que roda e o desfazer](docs/screenshots/terminal-finding-pt.png)

![A tela Sistema operacional: versão, atualizações esperando, reinício, atualizações automáticas de segurança, dpkg e apt, pronto para atualizar e as ações](docs/screenshots/terminal-os-pt.png)

## Começo rápido

Precisa do Hermes Agent 0.23.1 ou superior. O Hermes carrega os plugins por perfil, então instale em cada perfil que você usa.

```bash
hermes plugins install sesantanajr/hermes-atlas-evolution --enable              # o perfil padrão
hermes -p <perfil> plugins install sesantanajr/hermes-atlas-evolution --enable  # cada perfil com nome
hermes evolution                                                                # o menu
```

Instalar carrega o plugin num gateway em execução; não precisa reiniciar. A primeira olhada lê até 14 dias do histórico do perfil, então ele não começa do zero. No Modo Auto o plugin depois se instala sozinho nos outros perfis que você usa. Para atualizar: [Atualização](docs/pt-BR/upgrading.md).

## Novidades da 0.8.5

- **Modo Auto, ligado por padrão.** O Atlas Evolution passa a cuidar de todos os perfis do Hermes de uma vez e faz sozinho o que é seguro e reversível; o resto vira sugestão com o motivo e uma tecla. Assistido e Manual continuam disponíveis.
- **Ambiente: todos os perfis, plugins e skills** (tela 14, aba Ambiente, `hermes evolution environment`). Lê o código dos plugins sem executá-lo para saber quais ferramentas cada um muda, e só aponta conflito quando dois plugins mudam a mesma ferramenta, dizendo qual vale, qual se perde e o que desligar (nunca um plugin de segurança). Encontra também plugins duplicados, removidos do catálogo do Hermes, arquivados ou sem manutenção, instalados e nunca carregados, versões novas no catálogo, skills sem uso, duplicadas, parecidas ou copiadas em todos os perfis, perfis parados e restos de instalações que falharam.
- **Base de conhecimento no banco do plugin.** O catálogo oficial de plugins do Hermes (mais de 2.300, com os removidos por segurança), o que o plugin sabe de plugins conhecidos e o playbook de cada situação, com a ação e o desfazer, ficam em tabelas do `evolution.sqlite3`.
- **Recomendações claras.** Cada achado diz o que foi visto, o que fazer, o comando que roda e como desfazer. A observação confusa "plugins disputam um hook" saiu: dois plugins em ferramentas diferentes não são conflito.
- **Manutenção automática.** Libera versões antigas de ferramentas (`hermes pm gc`) uma vez por semana. Todas as mudanças: [CHANGELOG em português](CHANGELOG.pt-BR.md).

## Comandos

Subcomandos de `hermes evolution`. `--lang en|pt` funciona com qualquer um.

| Comando | O que faz |
|---|---|
| *(nenhum)*, `menu` | O menu interativo, no seu próprio terminal. Sem terminal (um script, um pipe, o shell do agente), o comando sozinho mostra o `status`. |
| `status` | Se o plugin observa, aprende e protege este perfil, o que pede atenção, a saúde do Hermes e a do sistema, e todos os perfis deste Hermes. |
| `report [--days N]` | O resumo dos últimos N dias (7 por padrão), comparado com o período anterior. |
| `lessons list`, `lessons review`, `lessons approve <id>`, `lessons approve --all` | As lições, e a sua decisão sobre as que esperam. |
| `proposals list`, `proposals approve <id>`, `proposals reject <id>` | As propostas; aprovar uma mudança que o plugin consegue fazer a aplica com o próprio comando do Hermes. |
| `decisions`, `decisions undo <id>` | As suas decisões e as ações automáticas, do terminal, do dashboard e do piloto automático, e o desfazer de uma. |
| `care [check \| pm-doctor \| doctor \| fix \| gc]` | A saúde do Hermes, lida dos próprios arquivos dele; e as checagens e reparos do próprio Hermes. |
| `system [check \| plan \| upgrade \| repair \| report] [--full] [--skip PACOTE...]` | A atualização do sistema operacional (Linux com apt). |
| `environment [show \| scan \| catalog]` | Todos os perfis deste Hermes: plugins, skills, conflitos e o que fazer; `scan` olha de novo agora; `catalog` o catálogo de plugins do Hermes guardado no banco do plugin. |
| `schedule-digest [--deliver local\|<canal>]` | O resumo semanal como um job de cron do Hermes sem agente. |
| `lessons export`, `lessons import` | Lições compartilhadas com outro perfil ou máquina, depois de mostrar cada uma. |
| `learn` | Uma passada de aprendizado na hora. |
| `owner` | Cada canal de chat com os remetentes, e o comando que marca um como você. |

Todos os comandos e opções: [Comandos](docs/pt-BR/evolution-loop.md#comandos).

## Configurações

Ajuste na aba **Evolution** do dashboard, no menu (tela 6), no Hermes Desktop em Settings → Plugins, ou no `config.yaml` do perfil em `plugins.entries.hermes-atlas-evolution.settings`. Uma mudança vale em até um minuto, sem reiniciar o gateway.

| Opção | Padrão | O que faz |
|---|---|---|
| `lessons` | `auto` | `auto`: lições de modelos fixos valem quando os seus turnos encontram a falha, são medidas e aposentadas quando não ajudam; texto escrito por um modelo ou pelo agente espera você. `suggest`: toda lição espera você. `off`: só mede. |
| `mode` | `auto` | O que o plugin faz sozinho. `auto`: pensa e age por você em todos os perfis, só o que é seguro e pode ser desfeito (veja [O Modo Auto](#o-modo-auto-padrão)). `assist`: só as cópias idênticas da memória sozinho; o resto espera uma tecla. `manual`: mede e propõe; nada muda sozinho. |
| `owner_sender_ids`, `owner_platforms` | `[]` | Quem é você nos chats: os seus ids de remetente, e plataformas em que todo chat é seu (um bot privado, a sua própria interface). |
| `language` | `auto` | `en` ou `pt`. `auto` segue o idioma do perfil. |
| `reflector` | `true` | Rascunha lições com um modelo de IA, pelo Hermes, para falhas sem modelo fixo (no máximo 6 chamadas por dia); os rascunhos esperam você. |
| `autopilot_minutes` | `60` | A passada de aprendizado em segundo plano, no máximo uma vez por intervalo (15 a 1440 minutos; `0` = só à mão). |
| `runtime_guard` | `true` | Barra comandos do agente que parariam o gateway ou apagariam aquilo em que o Hermes roda. |
| `agent_tools`, `tool_hints`, `feedback_signals`, `slow_turn_seconds` | | Veja [Configuração e idioma](docs/pt-BR/evolution-loop.md#configuração-e-idioma). |

`owner_sender_ids`, `owner_platforms`, `agent_tools: true`, `runtime_guard: false` e `mode: auto` por cima de uma escolha mais restrita só valem vindos da configuração do Hermes, nunca do `settings.json` do próprio plugin, que as ferramentas de arquivo do agente conseguem gravar.

## Privacidade e segurança

- **O que ele guarda.** Contadores, classes de falha, ids de sessão com hash, lições e propostas, em `<HERMES_HOME>/evolution/`; nenhum texto de conversa, prompt, argumento ou saída de ferramenta. Uma cópia de um arquivo de memória quando uma entrada sai, só do dono; antes de uma atualização do sistema, a lista de pacotes e uma cópia do `/etc` numa pasta só do root, fora de todo perfil (`/var/lib/hermes-atlas-evolution/system/`).
- **O que ele lê**, só para ler: o histórico, os logs e os registros de skills do Hermes, os arquivos do gateway e do banco; o `config.yaml`, os plugins (manifesto, checkout git e código Python, analisado e nunca executado) e as skills de cada perfil; o catálogo de plugins que o Hermes guarda em cache; do sistema, o estado do gerenciador de pacotes ([lista completa](docs/pt-BR/hermes-integration.md#o-que-o-plugin-lê)).
- **Rede e modelo.** Nenhuma conexão própria e nenhuma telemetria. Só o refletor chama um modelo, pelo Hermes. O `hermes doctor`, rodado só com um clique seu, consulta as APIs dos provedores (e manda um ping de 1 token para um endpoint `/anthropic`); todo o resto aqui não gasta nada.
- **Decisões.** Só você decide, no seu próprio terminal ou num dashboard com login. O plugin muda os próprios dados; as próprias configurações com `hermes config set`; a memória do Hermes só para tirar uma repetição; skills só com `hermes curator lock`; plugins de qualquer perfil só com os comandos do próprio Hermes (`hermes -p <perfil> plugins disable|enable|install`); pastas de instalações que falharam movendo-as para a lixeira dele; o sistema só quando você roda a atualização dele. Cada mudança tem o desfazer ou a cópia.

Detalhes: [SECURITY.md](SECURITY.md).

## Documentação

- [O ciclo de evolução](docs/pt-BR/evolution-loop.md): lições, o refletor, ações automáticas, a memória do Hermes, o menu, todos os comandos e configurações.
- [Manutenção](docs/pt-BR/maintenance.md): a saúde do Hermes e a atualização do sistema operacional, passo a passo.
- [Integração com o Hermes](docs/pt-BR/hermes-integration.md): hooks, o painel do dashboard, a guarda de runtime, o que o plugin lê.
- [Atualização](docs/pt-BR/upgrading.md): atualizar o plugin.
- [Operações](docs/pt-BR/operations.md), [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [Mudanças](CHANGELOG.pt-BR.md).

## Licença

O Atlas Evolution é software livre e de código aberto, publicado sob a [Licença MIT](LICENSE). Você pode usar, copiar, modificar, mesclar, publicar, distribuir, sublicenciar e vender cópias, inclusive para uso comercial, desde que o aviso de copyright e o texto da licença sejam mantidos em todas as cópias. O software é fornecido como está, sem garantia de qualquer tipo. Contribuições são bem-vindas sob a mesma licença; veja o [CONTRIBUTING.md](CONTRIBUTING.md).

## Autor

Criado por **Sérgio Sant'Ana Júnior**. [LinkedIn](https://www.linkedin.com/in/sergiosantanacloud/)
