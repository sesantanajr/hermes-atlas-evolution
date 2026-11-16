# Mudanças

*In English: [CHANGELOG.md](CHANGELOG.md), with every version.*

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
