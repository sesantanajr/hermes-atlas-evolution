# Atualização

[English](../upgrading.md) | **Português (Brasil)**

## Atualizar o plugin

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

A maior parte das configurações vale em até um minuto. Três esperam o `hermes gateway restart` (no terminal: a próxima sessão): `runtime_guard`, `tool_hints` e `agent_tools` quando você o liga (desligar vale na hora).

Se um perfil ainda tem o plugin com o nome antigo `hermes-evolution`, rode `hermes -p <perfil> plugins remove hermes-evolution` e depois `hermes -p <perfil> plugins install sesantanajr/hermes-atlas-evolution --enable`.

O que mudou em cada versão: [Mudanças](../../CHANGELOG.pt-BR.md).
