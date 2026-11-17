# Atualização

[English](../upgrading.md) | **Português (Brasil)**

## Atualizar o plugin

```sh
hermes evolution update --all          # todos os perfis; um perfil fixado passa para o último commit
hermes evolution update                # escolha os perfis numa lista
hermes evolution update --list         # versão, commit e fixação de cada perfil
sh install.sh --all --yes --restart    # o mesmo sem o plugin carregado
hermes gateway restart
```

Manualmente, um perfil por vez:

```sh
hermes plugins update hermes-atlas-evolution                 # o perfil padrão
hermes -p <perfil> plugins update hermes-atlas-evolution     # um perfil com nome

# Todos os perfis com nome
for p in $(ls ~/.hermes/profiles); do hermes -p "$p" plugins update hermes-atlas-evolution; done

hermes gateway restart
sudo systemctl restart hermes-dashboard   # ou do jeito que você roda o dashboard
hermes evolution status
```

O `plugins update` pela linha de comando só troca os arquivos: não recarrega um gateway que já está rodando, então reinicie o gateway uma vez no fim. O dashboard mostra a aba **Evolution** quando relê os plugins; se ela não aparecer, reinicie o dashboard. (O Hermes recarrega um gateway em execução ao instalar, ligar e desligar, e quando a atualização é aplicada pelo Hermes Desktop, pelo dashboard ou por `/plugins update` num chat.) Se algo ainda estiver rodando (um turno, uma tarefa agendada do Hermes (cron), uma tarefa delegada), o reinício lista e pergunta `Interrupt them and restart the gateway? [y/N]`: responda N e rode de novo em alguns minutos, ou y para interromper. O `status` aponta um gateway que ainda roda código antigo.

**Todos os perfis de uma vez, sozinho.** No Modo Auto, o perfil que cuida do Hermes inteiro mantém a cópia do plugin em cada outro perfil no código dele: uma cópia fixada em outro commit, mais antiga, com outro código na mesma versão, ou deixada pela metade por uma atualização interrompida é levada ao commit do perfil líder com o comando do próprio Hermes (no máximo dois perfis por rodada, uma vez por situação, nova tentativa no dia seguinte se o Hermes recusar). Um perfil em que o plugin está desligado, ou em Assistido ou Manual, fica como está, e um link de desenvolvimento também. Nada novo é baixado sozinho: uma cópia sem fixação só é atualizada enquanto o último commit do repositório for o que o líder roda, então atualize o líder (`hermes evolution update`) e os outros acompanham. Cada sincronização fica no histórico (`hermes evolution decisions`); reinicie o gateway uma vez para carregar o código novo.

O Hermes mantém a fixação quando um plugin é instalado de novo sem `--ref` (ele reinstala o commit fixado), e o `plugins update` dele recusa uma cópia fixada. Por isso o `hermes evolution update`, o `install.sh` e o Modo Auto movem a fixação com `hermes -p <perfil> plugins install <url> --force --ref <commit>`, o jeito do próprio Hermes; o perfil continua fixado, agora no código atual.

A maior parte das configurações vale em até um minuto. Três esperam o `hermes gateway restart` (no terminal: a próxima sessão): `runtime_guard`, `tool_hints` e `agent_tools` quando você o liga (desligar vale na hora).

Se um perfil ainda tem o plugin com o nome antigo `hermes-evolution`, rode `hermes -p <perfil> plugins remove hermes-evolution` e depois `hermes -p <perfil> plugins install sesantanajr/hermes-atlas-evolution --enable`.

O que mudou em cada versão: [Mudanças](../../CHANGELOG.pt-BR.md).
