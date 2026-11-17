# Primeiros passos

[English](../en-US/getting-started.md) · **Português (Brasil)** · [Início da documentação](README.md)

Do zero a um Hermes que aprende com os próprios erros, em uns dez minutos.

## 1. O que você precisa

- **Hermes Agent 0.23.1 ou superior**, com `hermes --version` funcionando no seu terminal.
- `git` (o Hermes usa para instalar plugins).
- Mais nada: o plugin usa só a biblioteca padrão do Python.

## 2. Instalar

O Hermes carrega plugins **por perfil**. Escolha um dos dois jeitos.

**Um perfil** (o padrão, ou um com nome usando `-p`):

```bash
hermes plugins install sesantanajr/hermes-atlas-evolution --enable
hermes -p trabalho plugins install sesantanajr/hermes-atlas-evolution --enable
```

**Todos os perfis de uma vez**, com o instalador:

```bash
git clone https://github.com/sesantanajr/hermes-atlas-evolution.git
sh hermes-atlas-evolution/install.sh                 # lista os perfis e pergunta quais (Enter = todos)
sh hermes-atlas-evolution/install.sh --all --yes     # sem perguntas
```

O instalador também aceita `--profiles default,trabalho`, `--install-missing` (instala onde o plugin falta) e `--restart` (reinicia o gateway no fim, depois de conferir que o serviço dele consegue subir).

Instalar carrega o plugin num gateway em execução: não precisa reiniciar. O dashboard mostra a aba **Evolution** quando recarrega os plugins (reinicie o dashboard se a aba não aparecer).

## 3. A primeira hora

1. **Dois minutos depois de carregar**, o relógio do plugin começa a primeira **rodada de aprendizado** em segundo plano. Quer agora? Rode `hermes evolution learn`.
2. A primeira rodada **repassa até 14 dias** do histórico do perfil (sessões, chamadas de ferramentas, turnos), então lições e números não começam do zero.
3. Ela lê **todos os perfis** deste Hermes (plugins, skills, memória) e roda as **checagens do próprio doctor do Hermes** sem rede.
4. No **Modo Auto** (o padrão) ela já aplica o que é seguro e reversível: tira entradas repetidas da memória, trava uma skill que o curador reescreve sem parar, libera versões antigas de ferramentas, instala o plugin em outros perfis em uso. Tudo vai para o histórico com o seu desfazer.

Depois, olhe:

```bash
hermes evolution          # o menu (no seu próprio terminal)
hermes evolution status   # uma página: o que precisa de atenção, o Índice de evolução, a saúde
```

## 4. Deixe do seu jeito

**Escolha um modo.** O Auto age sozinho no que é seguro; o Assistido só tira repetições idênticas da memória; o Manual só mede e propõe.

```bash
hermes config set plugins.entries.hermes-atlas-evolution.settings.mode assist
```

**Diga quem é você nos chats.** A sua CLI, a TUI e o Desktop sempre contam como você. Para Telegram, Discord ou o servidor de API, marque o seu id de remetente ou um canal que seja só seu, para que as suas falhas ensinem e os seus chats recebam todas as lições:

```bash
hermes evolution owner                     # cada canal com os remetentes
hermes evolution owner add 123456789       # imprime o comando `hermes config set` que marca você
```

**Receba o resumo semanal no seu chat**, como tarefa agendada do Hermes, sem agente e sem tokens:

```bash
hermes evolution schedule-digest --deliver telegram
```

## 5. Toda semana

- **O Índice de evolução** no `status` (e na Visão geral do dashboard) diz se o agente está melhorando: uma nota de 0 a 100 com as partes e a tendência contra uma semana atrás; as partes mostram qual se mexeu.
- **As propostas** esperam uma tecla: `hermes evolution proposals list`, ou o menu. Aprovar uma que o plugin sabe aplicar roda o comando do próprio Hermes; recusar impede o Modo Auto de aplicá-la algum dia.
- **As lições** que um modelo rascunhou esperam você: `hermes evolution lessons review`.

## 6. Atualizar e remover

```bash
hermes evolution update --all      # este plugin em todos os perfis (um perfil fixado passa para o último commit)
hermes gateway restart             # uma vez, para carregar o código novo
```

Para tirar de um perfil: `hermes -p <perfil> plugins remove hermes-atlas-evolution`. Os dados ficam em `<pasta do perfil>/evolution/` até você apagar essa pasta.

**Próximo:** [O ciclo de evolução](evolution-loop.md) explica como tudo funciona · [Solução de problemas](troubleshooting.md) quando algo parecer errado.
