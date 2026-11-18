# Primeiros passos

[English](../en-US/getting-started.md) · **Português (Brasil)** · [Início da documentação](README.md)

Do zero a um Hermes que aprende com os próprios erros, em uns dez minutos.

## 1. O que você precisa

- **Hermes Agent 0.23.1 ou superior**, com `hermes --version` funcionando no seu terminal.
- `git` (o Hermes usa para instalar plugins).
- Mais nada: o plugin usa só a biblioteca padrão do Python.

## 2. Instalar

O Hermes carrega plugins **por perfil**. Escolha um dos jeitos abaixo.

**Um perfil, pelo catálogo de plugins do Hermes** (a versão revisada; para um perfil com nome, acrescente `-p <perfil>`):

```bash
hermes plugins install hermes-atlas-evolution
hermes plugins enable hermes-atlas-evolution
hermes -p trabalho plugins install hermes-atlas-evolution
hermes -p trabalho plugins enable hermes-atlas-evolution
```

**Um perfil, pelo GitHub** (acompanha este repositório, em vez da versão revisada do catálogo):

```bash
hermes plugins install sesantanajr/hermes-atlas-evolution --enable
hermes -p trabalho plugins install sesantanajr/hermes-atlas-evolution --enable
```

**Todos os perfis de uma vez**, com o instalador:

```bash
git clone https://github.com/sesantanajr/hermes-atlas-evolution.git
sh hermes-atlas-evolution/install.sh --install-missing                          # lista os perfis e pergunta quais (Enter = todos)
sh hermes-atlas-evolution/install.sh --all --yes --install-missing              # todos os perfis, pelo GitHub, sem perguntas
sh hermes-atlas-evolution/install.sh --all --yes --install-missing --catalog    # o mesmo, pelo catálogo de plugins do Hermes
```

O instalador só roda comandos do próprio Hermes: `plugins update hermes-atlas-evolution` onde o plugin está e, com `--install-missing`, `plugins install` onde ele ainda não está (pelo GitHub, ou pelo nome no catálogo com `--catalog`). Uma cópia fixada num commit fica como está, com o comando que a move. Ele também aceita `--profiles default,trabalho` e `--restart` (reinicia o gateway no fim, depois de conferir que o serviço dele consegue subir).

Instalar carrega o plugin num gateway em execução: não precisa reiniciar. O dashboard mostra a aba **Evolution** quando recarrega os plugins (reinicie o dashboard se a aba não aparecer).

## 3. A primeira hora

Todo comando diz o perfil a que se refere: `hermes -p default evolution …` para o perfil padrão, `hermes -p trabalho evolution …` para o `trabalho`. Abaixo, `<perfil>` é o seu.

1. **Dois minutos depois de carregar**, o relógio do plugin começa a primeira **rodada de aprendizado** em segundo plano. Quer agora? Rode `hermes -p <perfil> evolution learn`.
2. A primeira rodada **repassa até 14 dias** do histórico do perfil (sessões, chamadas de ferramentas, turnos), então lições e números não começam do zero.
3. Ela lê **todos os perfis** deste Hermes (plugins, skills, memória) e roda as **checagens do próprio doctor do Hermes** sem rede.
4. No modo **Assistido** (o padrão) ela só tira uma entrada repetida, palavra por palavra, nos dois arquivos de memória, depois de guardar uma cópia do arquivo. O resto vira proposta e espera você. No **Modo Auto**, que você liga, ela também aplica o que é seguro e reversível: trava uma skill que o curador reescreve sem parar, libera versões antigas de ferramentas, move para a lixeira pastas de instalações que falharam, repara o serviço do gateway quando o Hermes removeu o Python que ele iniciava e instala o plugin em outros perfis em uso (sozinho, só pelo catálogo de plugins do Hermes, pelo nome). Tudo vai para o histórico com o seu desfazer.

Depois, olhe:

```bash
hermes -p <perfil> evolution          # o menu (no seu próprio terminal)
hermes -p <perfil> evolution status   # uma página: o que pede atenção, o Índice de evolução, a saúde
```

## 4. Deixe do seu jeito

**Escolha um modo.** O Assistido (o padrão) só tira uma entrada repetida, idêntica, nos dois arquivos de memória; o Auto age sozinho no que é seguro e tem desfazer, em todos os perfis deste Hermes; o Manual só mede e propõe.

```bash
hermes -p <perfil> config set plugins.entries.hermes-atlas-evolution.settings.mode auto
```

**Diga quem é você nos chats.** A sua CLI, a TUI e o Desktop sempre contam como você. Para Telegram, Discord ou o servidor de API, marque o seu id de remetente ou um canal que seja só seu, para que as suas falhas ensinem e os seus chats recebam todas as lições:

```bash
hermes -p <perfil> evolution owner                     # cada canal com os remetentes
hermes -p <perfil> evolution owner add 123456789       # imprime o comando `config set` que marca você
```

**Receba o resumo semanal no seu chat**, como tarefa agendada do Hermes, sem agente e sem tokens:

```bash
hermes -p <perfil> evolution schedule-digest --deliver telegram
```

## 5. Toda semana

- **O Índice de evolução** no `status` (e na Visão geral do dashboard) diz se o agente está melhorando: uma nota de 0 a 100 com as partes e a tendência contra uma semana atrás; as partes mostram qual se mexeu.
- **As propostas** esperam uma tecla: `hermes -p <perfil> evolution proposals list`, ou o menu. Aprovar uma que o plugin sabe aplicar roda o comando do próprio Hermes; recusar impede o Modo Auto de aplicá-la algum dia.
- **As lições** que um modelo rascunhou esperam você: `hermes -p <perfil> evolution lessons review`.

## 6. Atualizar e remover

```bash
hermes -p <perfil> evolution update --all      # o `plugins update` do próprio Hermes em todos os perfis, este primeiro
hermes gateway restart                         # uma vez, para carregar o código novo
```

O plugin nunca se atualiza sozinho: código novo só vem do Hermes (`plugins update`, o "Atualizar todos" do Desktop e do dashboard, ou `plugins.auto_apply`). Uma cópia que o Hermes fixou com `--ref` só passa para o commit do perfil em que você roda o `evolution update` ([Atualização](upgrading.md)).

Para tirar de um perfil: `hermes -p <perfil> plugins remove hermes-atlas-evolution`. Os dados ficam em `<pasta do perfil>/evolution/` até você apagar essa pasta.

**Próximo:** [O ciclo de evolução](evolution-loop.md) explica como tudo funciona · [Solução de problemas](troubleshooting.md) quando algo parecer errado.
